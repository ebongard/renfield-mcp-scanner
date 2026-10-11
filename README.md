# renfield-mcp-scanner

A USB document scanner as an ingest producer for Renfield: scan a stack, correct
it, route it to **exactly one** instance, push it through the existing
folder-ingest seam. Dedup, tier, PDF-split, OCR and filing are all unchanged —
this server sits in front of them.

Design: `renfield/docs/design/scanner-ingest.md`.

## The invariant

> A scan enters exactly one instance, and only after its destination is settled.
> An unrouted scan never transits any instance's database.

Instances are separate trust boundaries, and ingest is irreversible in practice:
within seconds a document has become chunks, embeddings, extracted facts and a
filed copy. So there is **no default target**. A scan that cannot be routed
waits in staging on this host for a human.

## Targets are 1..n, and they are configuration

`SCANNER_TARGETS_YAML` points at the registry. Adding a target is a config
change, never a code change. **`n = 1` short-circuits the whole routing layer** —
no classifier, no separator sheets, no review queue.

No tokens live in the registry: a target names the env var holding its token
(`token_env`), resolved at push time.

## Status

Phases 1 and 2. Declared-intent routing, the `n = 1` short-circuit, real hardware
status, durable staging, retry — and **barcode separator sheets**: a stack is
split at each sheet and each segment routed by the sheet that opened it.

Phase 3 as well: content classification behind a confidence gate, with a human
review floor beneath it (`route_scan`) and an append-only decision log
(`routing_audit`).

The classifier mirrors Renfield's own `services/simba_classify.py` — one
strict-JSON call, the answer validated against an ALLOWLIST of configured target
ids, best-effort throughout — rather than sharing its code, which lives in the
backend. One difference governs everything: simba_classify prefills a picker and
a human corrects it, while this one files a document across a trust boundary
with nobody looking. So the threshold is far stricter (0.95), and every
ambiguity — unreachable model, unparseable answer, invented id, low confidence —
resolves to "undecided", never to a guess.

Measured against a real model: a clearly company-addressed letter scored 0.90
and still went to the review queue. That is the intended cost, not a defect.

Set `SCANNER_CLASSIFIER_URL` / `SCANNER_CLASSIFIER_MODEL` to enable it; empty
disables L3 and an undecided scan simply waits for a human.

**Scan jobs.** `scan_document` starts a job and returns `{job_id, status:
running}` at once; the scan runs in the background. The scan used to run inside
the tool call, and on 2026-09-14 that went wrong three ways at once. Renfield's
30 s call timeout reported a scan as failed that had been filed. A longer timeout
only let a refresh tear the call down, and the automatic retry then scanned an
empty feeder. And OCR ran on the event loop, freezing the server for 12 s.

- **How a job ends.** When it finishes, the scanner tells the instance that ASKED,
  by posting `POST /api/scanner/job-event`. That is an event with capped backoff,
  never a poll. The caller is known from its Bearer token.
  `SCANNER_CALLER_TARGET_<CALLER>` names that caller's target, and the target's
  base URL and push token carry the event.
- 🛑 **A 404 is final only when it carries the application's marker.** The
  application answers `{"detail": {"reason": "folder_ingest_disabled", …}}` when
  folder ingest is off, and only THAT 404 stops the retries
  (`contract.INGEST_DISABLED_REASON`). Every other 404 — a bare body, a `detail`
  that is a string, a foreign reason — is retryable, because the same number
  comes from the ingress while no pod is ready. Measured on 2026-10-11 during a
  rollout: two 503 were retried correctly, then a Traefik 404 discarded the
  completion event for good. That it never reached the application is proven —
  `FOLDER_INGEST_ENABLED` was `true` and the backend log has no `job-event` line
  for that second. 401/403 stay final: both arise in the application and are
  unambiguous. Transitional consequence, accepted deliberately: against a
  Renfield build that does not send the marker yet, a switched-off ingest is
  retried for the whole retry horizon.
- **One scan at a time.** There is one feeder, so a second request while a scan
  runs gets the running job's id back.
- **Restarts.** Job records live under `<staging>/jobs`. A restart closes cut-off
  jobs as `interrupted` and re-sends events that never arrived.
- **OCR off the event loop.** OCR and PDF assembly run via `asyncio.to_thread`.
- **Job records off the event loop, atomically.** Every read and write of a job
  record runs in a worker thread. A write goes to a unique temp file, is fsynced,
  then renamed over the record. Writes run one at a time in call order, and the
  record is serialized when `save` is called, so a slow earlier write can never
  overwrite a newer state.
- **Cleanup while running.** A finished job's record is dropped once its event
  has settled and `SCANNER_STAGING_RETENTION_DAYS` have passed. The sweep runs
  whenever a job's event settles, and once at startup. It is not on a timer: the
  directory only grows when a job finishes, so sweeping then keeps it bounded
  without waking up on days nobody scans. A record with an open event is never
  dropped.
- **Bounded delivery after an outage.** At most `SCANNER_JOB_EVENT_CONCURRENCY`
  (default 2) events are in flight per target at once. Only the HTTP request
  holds a slot, not the backoff sleep. The backoff is jittered so open events do
  not retry in lockstep, and a `Retry-After` on 429/503 is honoured up to the
  5-minute backoff cap. The 24 h retry horizon is unchanged.

## Ein wartender Stapel fordert eine ENTSCHEIDUNG an

🛑 Gemessen am 2026-10-11: ein Stapel mit `reason: scanner_fault`
(`Document feeder jammed`, zwei Seiten, 20 MB) lag **sieben Tage** im
Zwischenlager. Zwei digitalisierte Seiten eines Briefes, die nie in einer
Instanz ankamen — und gemeldet wurde er allein als Zahl in der Startzeile
(„2 target(s), 1 pending, 0 purged"). Eine Zahl in einer INFO-Zeile liest sich
wie ein Betriebszustand, nicht wie eine offene Entscheidung.

`list_pending_scans` hat solche Stapel nie verschwiegen — `reason`, `detail`
und `at` standen immer im Eintrag (nachgemessen, bevor etwas geändert wurde).
Gefehlt haben das abgeleitete **Alter** (`age_seconds`) und die Unterscheidung
vom vorgesehenen Wartezustand: `fault` ist wahr für alles, was nicht
`unrouted` ist. Ein `unrouted`-Stapel wartet auf einen Menschen; ein
Fehler-Stapel wartet darauf, dass jemand überhaupt von ihm erfährt. Beide
abgeleiteten Felder stehen NACH den Metadaten aus der Datei, damit eine fremde
`stage.json` das Urteil nicht umschreiben kann.

Beim Start wird je Stapel, der länger als `SCANNER_PENDING_WARN_DAYS` (Vorgabe:
ein Tag, `0` schaltet ab) liegt, eine **Warnung** mit Alter, Grund und
Seitenzahl geschrieben. Je Start genau einmal — kein Takt, kein Wecker, sondern
die Gelegenheit, bei der der Zustand ohnehin gelesen wird.

**Die Aufbewahrung greift auch für Fehler-Stapel**, und zwar ohne Unterschied
nach Grund: `SCANNER_STAGING_RETENTION_DAYS` ab der letzten Änderung des
Verzeichnisses, also ab dem letzten `keep`. Ein Test hält das fest. 🛑 Gekehrt
wird beim START des Dienstes — ein Dienst, der monatelang durchläuft, kehrt
nicht aus. Auf dem Bedienrechner ist das in der Praxis kein Zeitraum (er
schläft, startet neu, wird aktualisiert), aber es ist eine Annahme und keine
Zusage.

## Separator sheets

`generate_separator_sheets` renders one printable A4 sheet per configured
target. They are generated **from the registry, never by hand** — a sheet whose
payload has drifted from the configuration fails silently: the stack scans,
nothing matches, and the documents land unrouted with no obvious cause.

The payload is namespaced and versioned — `RFSEP1:<target_id>` — because real
documents carry barcodes of their own (a GiroCode on an invoice, a tracking code
on a parcel notice). Encoding a bare label would let any of those masquerade as
a separator and silently split a document in half.

A sheet marks a boundary **and** a destination with one mark, which is what lets
a mixed stack be split and routed in a single pass. It is also the only routing
layer that survives an unattended scan, since a button press carries no intent.
Requires `zbar` (`brew install zbar`) and the `barcode` extra.

## Der blaue Knopf

Ein Druck am Gerät startet einen Scan — dieselbe Strecke wie `scan_document`,
nur ohne Agenten. Ein Hardwareknopf sendet kein Ereignis, also wird der
SANE-Sensor gepollt; das ist die einzige verfügbare Bauform, nicht Bequemlichkeit.

Zwei Takte, beide gemessen: ohne Papier im Einzug eine Abfrage je Sekunde (dort
kann ohnehin nichts starten), mit Papier alle 200 ms. Der Knopf meldet einen
Impuls von rund 200 ms je Druck — er verriegelt nicht und meldet kein Halten,
länger drücken bringt also nichts.

🛑 **Der Takt ist nach OBEN und nach UNTEN begrenzt.** Zehn Abfragen je Sekunde
belegten den Scanner zu 42 % der Zeit und setzten ihn nach einer halben Stunde
fest (SANE fand ihn nicht mehr, erst Aus- und Einschalten half). Die teure
Ressource ist hier das Gerät, nicht der Prozessor. `tests/test_button.py` hält
beide Grenzen fest.

Ein Knopfdruck trägt keine Absicht: das Ziel bestimmt der Klassifizierer, und
ein Trennblatt sticht ihn aus. Die FERTIGMELDUNG braucht dagegen eine feste
Adresse — ohne `SCANNER_CALLER_TARGET_BUTTON` läuft der Scan durch, aber niemand
erfährt davon. Abschaltbar mit `SCANNER_BUTTON_WATCH=false`.

🛑 **Der Wächter läuft unter Aufsicht, und sein Ende ist hörbar.** Bis zum
2026-10-11 lief er als nackte Task: `watch_button` fängt alles Vorgesehene ab
(fehlendes Gerät, Sensorfehler, abgelehnter Scan-Start), aber eine
UNVORHERGESEHENE Ausnahme beendete die Aufgabe endgültig — lautlos, weil die
starke Referenz in `server.py` Pythons Warnung „Task exception was never
retrieved" unterdrückte: die feuert erst beim Aufräumen, und aufgeräumt wurde
nie. Gemessen am Haushalt: nach dem Papierstau vom 2026-10-04 15:24 schrieb der
Wächter **sieben Tage** keine Zeile, obwohl sein Fehlerpfad gedrosselt eine
Warnung je Minute protokolliert — ein fehlendes Gerät hätte Tausende erzeugt.
Der blaue Knopf war eine Woche tot, und nichts hat es gesagt.

Jetzt startet `supervise_button` die Schleife nach einem Absturz neu und ein
Abschluss-Rückruf protokolliert jedes Ende der Aufgabe. `asyncio.CancelledError`
läuft durch: sie ist das geordnete Herunterfahren.

🛑 **Die Wartezeit ist ABGELEITET, nicht gewählt**:
`RESTART_BACKOFF = REDISCOVER_AFTER * ERROR_BACKOFF` (15 s), wachsend, gedeckelt
auf 60 s. Grund: ein neu gestarteter Wächter beginnt mit `device = None` und
ruft sofort `find_device`. Produktiv ist `config.device` LEER — `SCANNER_DEVICE`
setzt niemand —, also läuft dort `scanimage -f`, die vollständige SANE-Suche,
die den USB-Anschluss belegt (am 2026-10-11 gemessen: 4,3 s je Suche). Eine
Aufsicht, die schneller neu startet als die Bremse, die sich `watch_button`
selbst setzt, würde genau den Takt erzeugen, der den Scanner am 2026-09-25
festgesetzt hat. Aus „Knopf tot" würde „Gerät fest" — eine Reparatur, die
schlimmer ist als der Fehler. Zwei Tests halten die Grenze UND ihre Ableitung
fest; eine gleich große Zahl wäre Zufall und driftet beim nächsten Umbau.

🛑 **`scanner_status` sagt, ob der Wächter LEBT — und die Antwort hängt an
einer Zeitmarke, nicht an einer Task.** Nach dem Ausfall oben ist der Tod des
Wächters laut (eine ERROR-Zeile mit Rückverfolgung), aber er meldet sich nicht:
eine Protokollzeile wartet, bis jemand nachsieht, und am 2026-10-04 hat sieben
Tage niemand nachgesehen. Das Feld `button_watcher` macht ihn auffindbar — für
eine funktionale MCP-Probe, die das Werkzeug ohnehin aufrufen kann.

Die Falle, die dabei zählt: die Task EXISTIERTE in diesen sieben Tagen. Jede
Auskunft, die daraus abgeleitet wäre, hätte „läuft" gemeldet. Also setzt die
Schleife bei jedem Durchlauf selbst eine Marke, und nur deren Alter entscheidet.

Die Grenze ist ABGELEITET, Summand für Summand:
`LIVENESS_WORST_GAP = RESTART_BACKOFF_MAX + 2 × sane.PROBE_TIMEOUT +
ERROR_BACKOFF` — die Wartezeit der Aufsicht nach einem Absturz, plus eine
hängende Gerätesuche, plus ein hängender Sensorabruf, plus die Wartezeit am
Ende des Durchlaufs. Darauf `LIVENESS_FACTOR` (zwei), damit ein einzelner
unvorhergesehener Halt noch keinen Fehlalarm ergibt. Die beiden SANE-Grenzen
sind der Grund und standen in der ersten Fassung nicht in der Rechnung
(Review-Befund L2): ein hängendes `scanimage` zieht die Lücke über die
Wartezeit der Aufsicht hinaus, ohne dass der Wächter steht. Die Zeitgrenze
steht darum nur EINMAL als Zahl, in `sane.PROBE_TIMEOUT`.

🛑 Die Marke steht am **ENDE** jedes Durchlaufs, auch im Sensorfehlerpfad:
sie bedeutet „ein Durchlauf ist VOLLENDET", nicht „die Schleife wurde
betreten". Am Anfang gesetzt las ein Absturzkreis als `alive` — die Aufsicht
startet neu, der Wächter betritt die Schleife, die Marke ist frisch, und der
Knopf wirkt trotzdem höchstens zeitweise. Im Review vom 2026-10-11
nachgemessen.

Weil ein Kreis auch einzelne Durchläufe vollenden kann, ist die Marke allein
nicht genug: eine FRISCHE Neustartfolge (`CRASHLOOP_RESTARTS` Neustarts, der
letzte innerhalb der Grenze) meldet `state: "crashloop"` und ist ein Befund.
Beide Bedingungen sind nötig — die Aufsicht setzt die Folge nur in ihrem
Absturzzweig zurück, ein Dienst mit zwei Abstürzen beim Start trüge sie sonst
für immer.

Ein schlafender oder ausgestöpselter Scanner ist der NORMALFALL auf einem
Arbeitsplatzrechner und darf keinen Alarm erzeugen — darum trennt der Befund
Lebendigkeit und Gerät (`device_readable`), und `finding` ist **nur** bei
`dead` und `crashloop` gesetzt. `SCANNER_BUTTON_WATCH=false` meldet `state: "off"`, kein Befund. Und
der Befund reitet auf der FEHLERANTWORT mit: `scanner_status` kehrt bei
fehlendem Gerät früh zurück, und genau dann darf die Probe den Wächter nicht
aus den Augen verlieren.

| Feld | Bedeutung |
|---|---|
| `state` | `off` (absichtlich aus) · `starting` (noch kein vollendeter Durchlauf) · `alive` · `dead` · `crashloop` |
| `finding` | Wahr bei `dead` und `crashloop` — darauf schaut eine Probe |
| `last_tick_age_seconds` / `stale_after_seconds` | Alter der Marke und die Grenze, gegen die verglichen wurde |
| `since` | Wanduhrzeit, seit der die aktuelle Schleife läuft (nur Anzeige) |
| `restarts_total` / `restart_streak` | Neustarts der Aufsicht insgesamt, und der Zähler aus ihrer Meldung |
| `device_readable` | Ob der letzte Sensorabruf gelang — `false` ist KEIN Befund |

## Tools

| Tool | Purpose |
|---|---|
| `list_scanners` | The attached scanner, its settings, configured targets |
| `scanner_status` | Real hardware state from the SANE sensors — never inferred — plus whether the button watcher is alive |
| `scan_document` | START a scan job (feed, correct, route, push in the background); returns a `job_id` at once, `busy` while another scan runs |
| `scan_job_status` | How a job stands or ended — for an explicit question, not for polling |
| `list_pending_scans` | Scans held here awaiting a decision or a retry |
| `retry_pending_scans` | Re-send scans kept after a failed push |
| `generate_separator_sheets` | Render a printable sheet per configured target |
| `route_scan` | Decide where a waiting scan belongs and file it |
| `routing_audit` | Recent routing decisions and the reason for each |

## Install (macOS operator host)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
cp config/targets.example.yaml targets.yaml     # edit
SCANNER_TARGETS_YAML=targets.yaml ./.venv/bin/renfield-mcp-scanner-scan   # preflight
```

Then the LaunchAgent in `launchd/`. Preflight exits non-zero if the scanner is
unreachable or a token is unset, so a launch script can gate on it.

**The vendor scanner software must not be running or in Login Items.** It claims
the USB device exclusively, which blocks this server entirely.

## TLS to a self-hosted backend

A self-hosted Renfield often serves a private or self-signed certificate. `curl`
may accept it from the OS trust store while Python does **not** (httpx verifies
against certifi), so a push fails with `CERTIFICATE_VERIFY_FAILED` even though
the same URL works fine in a shell. Point the target's `ca_bundle` at the PEM.

Verification is never disabled — an unverified push would send documents to
whatever answers the hostname.

## Two credential directions, both per-party

| variable | direction | granularity |
|---|---|---|
| `SCANNER_TOKEN_<TARGET>` | scanner → Renfield (pushing documents) | one per target |
| `SCANNER_MCP_TOKEN_<CALLER>` | Renfield → scanner (calling tools) | one per caller |
| `SCANNER_CALLER_TARGET_<CALLER>` | scanner → the calling Renfield (job completion events) | names the caller's target; reuses that target's push token |

Both are per-party for the same reason: a shared secret cannot be revoked for
one party without breaking the others. The server refuses to bind a non-loopback
address unless at least one caller token is configured.

## Scan quality

Six defects were measured and fixed once, in `pipeline.py`, `pdf.py` and
`sane.py`, rather than rediscovered: SANE defaults to US Letter and eats 17.7mm
off every A4 page; ocrmypdf's default optimizer transcodes to lossy JPEG; never
deskew twice; scanner-side `--swdeskew` *creates* skew on sparse back pages;
raw colour is strongly blue; duplex show-through needs explicit clearing. Each
carries its measurement in a comment at the point it is handled.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```
