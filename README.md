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
  unambiguous. 🛑 The marker is NOT `reason: "feature_disabled"` — that one is
  generic (two other Renfield routes use it for two different features) and it
  rides on a **503**, i.e. on a retryable answer. A reason that foreign routes
  use for "not now" must never mean "forever"; a test pins that the scanner
  refuses it as a marker (review finding B2). Transitional consequence, accepted deliberately: against a
  Renfield build that does not send the marker yet, a switched-off ingest is
  retried for the whole retry horizon — see the next bullet for what bounds it.
- 🛑 **A blank 404 is retried for `SCANNER_JOB_EVENT_RETRY_HOURS` IN TOTAL, not
  per service start.** Retrying is the safe side of the 404 split, but it
  amplifies: `gave_up` events are re-sent on every start with a fresh attempt
  budget and only expire with `SCANNER_STAGING_RETENTION_DAYS`, so a wrong
  `base_url` in `SCANNER_CALLER_TARGET_<CALLER>` — which answers a blank 404,
  indistinguishable from a rollout's — would be asked ~300 times per start for
  up to 30 days, where it used to cost one ERROR line and stop (review finding
  A1). The bound: the time of the FIRST blank 404 is written into the record
  (`refused_since`, wall clock, so it survives a restart), and once the span is
  exceeded the event settles as `gave_up_404` — a state that is never re-sent
  again. The separator is the duration: a blank 404 on a configured address is
  either a rollout (seconds) or a misconfiguration (forever). Any other answer
  in between (a 503, a transport error) clears the judgement — whoever answers
  503 is reachable, so the address is right. The **first** blank 404 per job is
  a WARNING naming the target URL and `SCANNER_CALLER_TARGET_*`; the rest are
  INFO, because 300 identical warnings are none (review finding A2).
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

🛑 **AUS als Vorgabe seit 2026-10-26 (`SCANNER_BUTTON_WATCH`), weil er den
Scanner festsetzt.** Auf dem ScanSnap S1500 schließen sich „Knopf jederzeit"
und „verklemmt nie" gegenseitig aus. Drei Messungen, die zusammen keinen
Ausweg lassen:

| Messung | Folge |
|---|---|
| Der Impuls dauert 200–250 ms und **hält nicht** | Takt ≤ 200 ms, sonst gehen Drücke verloren |
| Jede Abfrage ist ein `scanimage -d … -A`, also ein voller Öffnungsvorgang | der Takt ist Gerätelast, nicht Prozessorlast |
| Die IORegistry trägt **keine** Knopf-Eigenschaft (2026-10-26 nachgesehen) | kein freies Signal, das das Öffnen ersetzen könnte |

Dazu eine Messung gegen die bequeme Erklärung: im verklemmten Zustand steht
`CurrentPowerState == MaxPowerState` (`ioreg`, 2026-10-26). Das schließt
**USB-Suspend** aus — der Bus hält das Gerät bei voller Leistung, es ist also
nicht der Rechner, der es schlafen legt. ⚠️ Ein **geräteinternes** Standby
schließt es NICHT aus; dafür gäbe es `Sensors.power_save` aus `sane.py`, und
das ist im verklemmten Zustand nicht abfragbar — jede Abfrage bräuchte genau
die Öffnung, die scheitert. Die Auto-Abschaltung ist damit unwahrscheinlich
gemacht, nicht widerlegt.

Verschärfend, lange übersehen: der Takt hängt an `sensors.page_loaded`
(`button.py:493`), mit der Begründung „ohne Papier kann der Knopf ohnehin
nichts auslösen". Der Entwurf nahm an, Papier liege nur kurz vor einem Scan
ein. Wer den Deckel offen und Papier dauerhaft eingelegt lässt — der
Normalfall beim Eigentümer — bekommt nach dem Code **fünf Öffnungen je
Sekunde** statt einer. ⚠️ Gefolgert, nicht gemessen: die Episoden, auf die
sich die Zahlen unten stützen, liefen ausdrücklich „NUR `read_sensors`
(~1/s)", also ohne Papier im Einzug.

⚠️ **Die DOSIS ist nicht belegt, und zwei Messungen widersprechen sich.** Zehn
Abfragen je Sekunde setzten das Gerät nach rund einer halben Stunde fest
(≈ 18.000 Öffnungen); eine Abfrage je Sekunde nach rund 14 Minuten. Letzteres
ist am Protokoll nachgemessen (2026-10-26): **fünf Episoden im Band
842–872 s**, Mittel ≈ 863 s, Streuung ≈ 11 s — alle am 2026-10-21.

Das ist in die FALSCHE Richtung: ein reiner Öffnungszähler müsste bei höherem
Takt FRÜHER klemmen. Und die enge Streuung über fünf Läufe passt ebenso gut zu
einem **Uhrwerk** (fester Zeitgeber um 14,5 min) wie zu einer Dosis bei
konstantem Takt — alle fünf stammen vom selben Tag, also vermutlich vom selben
Takt. **Diese Zahl unterscheidet die beiden Erklärungen nicht.**

🛑 **Dass das Abschalten hilft, ist plausibel und NICHT bewiesen.** Gilt die
Dosis, hilft es vollständig. Gilt das Uhrwerk, verklemmt das Gerät weiter —
dann allerdings in einem Zustand, in dem auch ein angeforderter Scan
scheitert, und das wäre vorher wie nachher so. Der entscheidende Versuch ist
billig: nach einem Aus/Ein mehr als 15 Minuten nichts tun, dann EINEN Scan
anfordern. Klappt er, war es die Dosis.

Solange das offen ist, taugt keine Zahl als Grundlage für einen „sicheren"
Takt — und genau deshalb ist der Wächter aus und nicht langsamer gestellt.

Der Wächter (`server.py:310`) ist die einzige periodische Aufgabe, die das
Gerät anfasst; alles andere öffnet nur auf Anforderung. **Aus heißt: im
Ruhezustand null Öffnungen.** Gescannt wird über einen angeforderten Scan
(Chat, Weboberfläche, `scan_document`). `watcher_health` meldet dann
`state: "off"` mit `finding: false` — kein Daueralarm, die Abschaltung ist
eine Entscheidung und wird als solche gemeldet.

`SCANNER_BUTTON_WATCH=true` schaltet ihn wieder an: für Geräte, deren Sensor
den Druck hält, oder um die Dosis sauber auszumessen. Auf diesem Modell
verklemmt er dann wieder.

Alles Folgende beschreibt, wie der Wächter arbeitet, WENN er eingeschaltet
ist — die Maschinerie bleibt vollständig erhalten.

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
erfährt davon. Der Schalter ist `SCANNER_BUTTON_WATCH` (Vorgabe: aus, s. oben).

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

### 🛑 Ein TOTER Wächter ist laut. Ein BLINDER war stumm.

Die Lebendigkeit oben beantwortet „läuft die Schleife?" — nicht „sieht sie
etwas?". Bis 2026-10-21 fielen die beiden Fragen auseinander, und die zweite
hatte keine Antwort:

`read_sensors` verwarf den Exit-Status von `scanimage`. Ein gescheiterter Abruf
ergab `raw={}`, daraus alle Sensoren `False`, `error_code=0` — und damit
`ready is True`. Keine Ausnahme. Also: `note_device(True)`, die Marke tickt,
`device_readable` liest `true`, `errors` bleibt 0, `device` bleibt gesetzt,
`find_device` läuft **nie wieder**. Der Wächter meldet „bereit, kein Papier"
und tut das beliebig lange.

**Gemessen am Haushalt:** der Prozess lief am 2026-10-21 seit neun Tagen ohne
Neustart, und die letzte Zeile irgendeines Scanner-Bestandteils war vom
2026-10-12 11:57. Ein Knopfdruck erzeugte keine Zeile, keinen Auftrag, nichts
im Backend. Der blaue Knopf war wieder eine Woche tot — diesmal mit lebender
Aufsicht, grüner Lebendigkeit und `device_readable: true`.

Das ist NICHT der Ausfall vom 2026-10-04 (Task gestorben, Aufsicht fehlte); es
ist sein Gegenstück, und #5/#6 greifen dagegen nicht. Was jetzt gilt:

* `read_sensors` wirft auf **zwei getrennten Bahnen** — Exit-Status ≠ 0
  (Backend-Fehler) und leeres Ergebnis bei Status 0 (Regex-/Formatfehler). Je
  ein eigener Test: deckte eine die andere mit ab, wäre hinterher nicht
  erkennbar, welche gefeuert hat.
* `Sensors.ready` ist bei leerem `raw` **`False`** — für jede Herkunft eines
  Satzes, nicht nur für `read_sensors`.
* Damit wird `device_readable` zum ersten Mal wahrheitsfähig, und der
  Fehlerpfad greift überhaupt.

🛑 **Was das NICHT behebt.** Der Fehlerpfad kennt zwei Fälle, und sie verhalten
sich entgegengesetzt:

| Fall | was scheitert | `device` | wirkt `REDISCOVER_AFTER`? |
|---|---|---|---|
| **A** | `find_device` | bleibt `None` — die Zuweisung findet nicht statt | **nein**, `device = None` ist dann ein Leerlauf |
| **B** | `read_sensors` | gesetzt | ja |

Die acht Episoden vom 2026-10-11 waren **alle Fall A** (462 Warnzeilen, alle
mit der Meldung aus `find_device`): gemessen 4956 Versuche über 15,3 h, 11,11 s
je Versuch, und Suche (6,17 s) + `ERROR_BACKOFF` (5,0 s) = 11,17 s — die
Vollaufzählung steckte in **jedem** Versuch, und kein Zähler ändert daran
etwas. Fall B war in 33 Tagen nie aufgetreten, weil `read_sensors` nicht laut
scheitern KONNTE. Die Rationierung (`since_rediscover`, zurückgesetzt beim
Auslösen) gehört deshalb hierher, aber aus dem umgekehrten Grund: die
Fehlschlag-Erkennung macht Fall B zum **ersten Mal erreichbar**, und ohne sie
entstünde damit ein NEUER Dauerzustand mit Suche in jedem Takt. Ein eigener
Test nagelt die Gegenbehauptung für Fall A fest (M Takte → M Suchen), damit
„der Riegel ist repariert" nicht später für seine Behebung gehalten wird.

### Die Proben sind serialisiert — und wie weit das reicht

Fünf Aufrufer lösen `find_device` unabhängig aus (`scan.py`, `button.py`,
`tools.py` dreimal), und `config.device` ist produktiv LEER, also sucht jeder
von ihnen voll. Zwei davon laufen regelmäßig gegeneinander, und das erklärt den
Befund „`scanner_status` scheitert, während der Wächter im selben Prozess
erfolgreich liest": nicht zwei Sichten auf einen Zustand, sondern zwei Öffner,
von denen einer verliert. Ein Zusammenstoß hat jetzt mit
`DeviceContendedError` eine eigene Klasse — sonst sieht er in der Auswertung
wie ein Verklemmen aus.

🛑 Drei Grenzen, damit niemand mehr erwartet als da ist:

* **Nur die Proben, nicht der Scan.** Ein Schloss über `scan_batch` würde bis
  zu 1800 s gehalten, `LIVENESS_STALE_AFTER` liegt bei 370 s — die Aufsicht
  erklärte den Knopf bei JEDEM langen Scan für tot. Probe gegen Scan zu
  serialisieren braucht eine Änderung am Wächter (Takt überspringen statt
  anstehen) und ist nicht gebaut.
* **Reichweite: ein Prozess, eine Ereignisschleife.** Das Schloss liegt je
  Schleife (ein `asyncio.Lock()` auf Modulebene bindet sich an die erste und
  wirft in jeder weiteren — im Dienst unsichtbar, im zweiten Test sofort). Je
  ein Aufrufer in einem eigenen Thread bekäme sein eigenes Schloss, und die
  Serialisierung wäre still weg.
* **Den sechsten Aufrufer erreicht es nicht:** ein `scanimage` von Hand,
  Chromes Geräteaufzählung. Eine GRENZE, keine Lücke.

Und `_run` räumt bei **jeder** Beendigung auf, vor allem bei
`CancelledError`. Vorher tötete nur die Zeitablauf-Bahn den Prozess; wurde die
wartende Aufgabe abgebrochen, blieb ein `scanimage` stehen, das das USB-Gerät
weiter hielt — der nächste Aufruf fand „no Fujitsu scanner found", ein
Fehlerbild, das wie ein Gerätedefekt aussieht und von uns stammt.

### `watcher_health` — das Werkzeug, auf das die Probe schaut

Die Lebendigkeit oben ist nur dann etwas wert, wenn jemand sie LIEST. Bis
2026-10-21 tat das niemand: der Scanner hatte im Backend gar keine
Funktionssonde, mit dieser Begründung in `mcp_servers.yaml` — „liest `down`,
wenn der Rechner schläft, erwartet, kein Fehler". Für den schlafenden Mac
stimmt das, und das Rahmenwerk überspringt Proben für getrennte Server
ohnehin. Nur hat dieselbe Begründung den zweiten Fall mitgedeckt: **Dienst
läuft, Knopf tot.** Neun Tage, mit lebender Aufsicht und grüner Lebendigkeit.

🛑 **Gesondet wird NICHT `scanner_status`.** Das fasst das Gerät an und gibt
bei fehlendem Gerät eine Fehlerantwort zurück — ein ausgeschalteter Scanner ist
der Normalfall, eine Probe darauf schlüge dauernd an und würde abgeschaltet.
Eine Probe, die am Normalfall anschlägt, ist schlimmer als keine.

`watcher_health` liest nur Kostenloses — die Zeitmarke des Wächters (im
Speicher) und den USB-Knoten über `ioreg` — und **fasst das Gerät nicht an**.
Nach dem 2026-10-21 ist das keine Nebensache: dort war der Scanner neun Tage am
Bus angemeldet (unveränderte `sessionID`) und für SANE unansprechbar, OHNE dass
eine einzige Gerätesuche gelaufen wäre. Eine Probe im Takt, die selbst öffnet,
wäre Last auf genau dem Anschluss, dessen Gesundheit sie messen soll. Ein Test
hält fest, dass sie SANE nicht aufruft — mit einer Gegenprobe, dass
`scanner_status` es sehr wohl tut.

| Wächter | `device_readable` | USB-Knoten | Ergebnis |
|---|---|---|---|
| `off` / `starting` | — | — | ok |
| `dead` / `crashloop` | — | — | **Befund** (`watcher_dead` / `watcher_crashloop`) |
| `alive` | `False` | **fehlt** | ok — Scanner aus, der Normalfall |
| `alive` | `False` | **da** | **Befund** (`device_wedged`) |
| `alive` | `False` | unbekannt | ok — eine fehlende Messung ist kein Fehler |
| `alive` | `True` / `None` | — | ok |

Die vorletzte Zeile ist die, die man vergisst: liefert `ioreg` keine Auskunft,
wird daraus KEIN Befund. Sonst tauscht man einen verschwiegenen Ausfall gegen
einen erfundenen.

Stanza im Backend: `health_probe: {tool: watcher_health, interval: 900,
expect: {min_items: 0}}`. Mit der Hysterese (2) meldet sich ein Befund nach
rund 30 Minuten.

### Ein laufender Scan ist kein Defekt

🛑 Beim ersten echten Knopfdruck nach dem Umbau gemessen (2026-10-21):

```
14:14:49  Knopf gedrückt — starte Scan (Papier: ja)
14:14:54  Sensor nicht lesbar (scanimage endete mit 1: open of device failed)
14:15:25  Gerät gefunden, Sensor wieder lesbar nach 4 Fehlversuch(en)
```

Der Scan hält den Einzug, also MUSS ein Sensorabruf daneben scheitern. Das
hatte zwei falsche Folgen: `watcher_health` sah `device_readable=False` bei
vorhandenem USB-Knoten und meldete `device_wedged` — ein Fehlalarm auf den
normalsten Zustand überhaupt —, und nach drei Fehlschlägen löste der
Fehlerpfad eine 6-Sekunden-Vollsuche aus, mitten im laufenden Scan.

Beide hängen an einer Frage, und die hat EINE Quelle: `sane.device_held()`,
ein Zähler in dem Modul, das das Gerät öffnet. Der Wächter überspringt den
Takt, und die Probe meldet `ok`.

🛑 **Die Quelle ist die PHASE, nicht der Auftrag — und das ist der Unterschied
zwischen begrenzt und unbegrenzt.** Die erste Fassung fragte
`JobManager._active`, also „läuft ein Auftrag". Das umfasst Zusammenbau, OCR
und Klassifizierung, und `img2pdf`/`ocrmypdf` laufen über `asyncio.to_thread`,
sind also **unabbrechbar**. Eine hängende OCR hätte den Auftrag für immer
„laufend" gehalten: die Probe dauerhaft verstummt, die Zeitmarke grün, der
Knopf tot — der Neun-Tage-Ausfall durch eine neue Tür. Im Review zu #10
gefunden.

Seit #11 tragen beide Aufrufe eine abgeleitete Zeitgrenze (`60 s +
20 s/Seite`, s. „Das Zeitbudget des Zusammenbaus"), unbegrenzt ist dort also
nichts mehr. **Die Wahl bleibt trotzdem richtig, und zwar deutlicher als
vorher.** Das Budget gilt je UNTERPROZESS, der Ausfall aber je AUFTRAG: ein
voller Einzug ergibt duplex 100 Seiten → `B` = 2060 s, und ein `assemble`
zahlt es zweimal (`img2pdf`, dann `ocrmypdf`) → **2·B ≈ 69 min**, das
11-Fache von `LIVENESS_STALE_AFTER` (370 s). Dazu kommt `correct_file`, das
weiterhin unbegrenzt je Seite läuft.

Eine Grenze, die zehnfach jenseits des Lebendigkeitsfensters liegt, taugt nicht
als Lebendigkeitsgrenze. Die Phase ist die Quelle, nicht der Auftrag — und
diese Zahl ist der Grund, warum das so bleiben muss.

**Bis #13 war es 4·B ≈ 2 h 17 min**, weil `assemble` ZWEIMAL über denselben
Stapel lief: der Klassifizierer baute eine Sonden-PDF, um Text daraus zu
lesen, und `_deliver` baute dieselbe PDF danach noch einmal. `route_scan` war
schlimmer — es baute bedingt („falls noch keine da ist") und liess `_deliver`
danach bedingungslos erneut bauen, der bedingte Aufruf war also vollständig
wirkungslos. Seither nimmt `_deliver` eine fertige PDF an (`pdf_path`): die
Sonden-PDF IST die Lieferung, und ein Stapel, der schon eine PDF trägt, wird
nicht neu gebaut. Die Tests ZÄHLEN die Aufrufe, sie behaupten keine Laufzeit.

⚠️ **Was die Wiederverwendung kostet, ausdrücklich entschieden:** scheitert die
Texterkennung, behält `assemble` das PDF ohne Textschicht (das ist der
gewollte Rückfall — ein Dokument geht nie verloren, weil die OCR scheiterte).
Vorher baute `route_scan` neu und versuchte die OCR dabei ERNEUT; jetzt wird
das textlose PDF unverändert ausgeliefert. Das ist bewusst so: ein zweiter
Versuch kostet den vollen Durchlauf und hat keinen Grund, anders auszugehen —
die Ursachen sind fehlende `tessdata`, eine unlesbare Seite oder ein
abgelaufenes Budget, und keine davon ändert sich durch Wiederholen. Wer die
Textschicht braucht, scannt neu. Im Review zu #13 gefunden und hier
festgehalten, damit es keine stille Verhaltensänderung bleibt.

Nebenwirkung des Doppellaufs, die niemand gesucht hatte: `output_name` stempelt
minutengenau, zwei Läufe über eine Minutengrenze hinterliessen also ZWEI PDFs
im Stapel — eine davon eine Waise. Und `correct_file` korrigiert an der Stelle,
lief also auch zweimal; gegengemessen verschiebt der zweite Durchlauf maximal
EINEN Helligkeitswert je Kanal, kostete somit Zeit und nicht Qualität.

Das Gerät wird nur während `scan_batch` gehalten, und das ist durch sein
eigenes `timeout=1800` begrenzt. **Unbegrenztes Schweigen ist damit
strukturell unmöglich, ohne eine gewählte Frist.** Nebengewinn: nach dem Scan
ist der Einzug frei, der Knopf ist während der OCR bedienbar statt unnötig
taub.

Ein **Zähler**, kein Schalter: ein Schalter, den zwei Pfade setzen und einer
zurücknimmt, bleibt irgendwann hängen. Er wird im `finally` geräumt, auch bei
einer Ausnahme — ein Test hält genau das fest, denn ein hängender Zähler *ist*
das unbegrenzte Schweigen.

🛑 **Der übersprungene Takt setzt die Zeitmarke trotzdem.** Ein Scan darf
1800 s dauern, `LIVENESS_STALE_AFTER` liegt bei 370 s — ohne die Marke
erklärte die Aufsicht den Knopf bei JEDEM langen Scan für tot. Genau der
Fehlalarm, den das Nicht-Sperren von `scan_batch` vermeiden soll.

🛑 **`pressed_before` bleibt im Überspring-Zweig UNBERÜHRT.** Es dort auf
`False` zu setzen erfände eine Flanke: endet der Scan, während der Knopf noch
gedrückt ist, läse der nächste Takt `pressed=True` bei `pressed_before=False`
und startete einen zweiten Scan — mit leerem Einzug. Ein Druck, den der
Wächter schon gesehen hat, ist EIN Druck.

### Die Fertigmeldung eines Knopf-Scans hat keinen Empfänger

Eine Fertigmeldung wird an die `session_id` des anfordernden Chats gebunden.
Ein Knopfdruck hat keine Sitzung — die Gegenseite antwortet `409 unknown job`,
und der Eintrag entsteht dort NIE.

🛑 Eine blanke 409 ist mit Absicht **wiederholbar**: ein sofort scheiternder
Scan kann melden, bevor der Anforderer notiert ist. Für den Knopf gilt das
nie. Das Ereignis führt deshalb `caller` mit, und nur bei `caller == "button"`
trägt die 409 `{"reason": "no_recipient_for_caller"}` — dasselbe Muster wie
bei der 404: **endgültig ist eine Ablehnung nur MIT Merkmal.**

Das DOKUMENT ist davon unberührt — es geht über den getrennten Ingest-Weg und
ist längst abgelegt, bevor die Meldung scheitert. Offen bleibt allein, WEM
eine Knopf-Meldung gälte; das Trennblatt entscheidet die Ziel-INSTANZ, nicht
den Empfänger darin.

### Die freie Diagnose am USB-Knoten

`usb.py` liest über `ioreg` die Registratur des Kernels. Das öffnet das Gerät
NICHT, belegt den Anschluss nicht und kann nichts verklemmen — darum darf es
neben dem Geräte-Budget stehen. Ein Aufruf je **Ereignis** (Fund, Fehlschlag),
nicht je Takt: er hängt an der gedrosselten Warnzeile.

Er trennt „Scanner aus" von „Scanner verklemmt" — beides ergab vorher
`ok:false`. Beim Stromlosmachen wechselt die `sessionID`, der SANE-Name
`:10551` bleibt; `locationID` ist das zweite Merkmal, weil ein Verbund- oder
Hub-Knoten mehr als einen Treffer liefern kann.

🛑 **Auswahl über `idVendor`/`idProduct`, NIE über Einrückung.** Die erste
Fassung las die Textausgabe von `ioreg` und trennte die Geräte an `^\+-o`;
verschachtelte Knoten sind eingerückt und fallen in den Block ihres
Elternteils. Der Treffer „ScanSnap" lag im Block eines Monitors, und die
gelieferte `sessionID` war **dessen** — eine plausible Zahl vom falschen Gerät,
die schlimmste Sorte Messwert, weil sie in der Auswertung nicht auffällt. Jetzt
über den XML-plist (`ioreg -a`), ein Baum mit eigenen Merkmalen je Knoten. Ein
Test hält fest, dass bei ABWESENDEM Scanner keine fremde Kennung zurückkommt.

🛑 **Die Grenze der Auskunft:** im verklemmten Zustand ist der Knoten
weiterhin VORHANDEN (gemessen). Sie ist ein Abbruch- und Diagnosesignal, kein
Aufwachsignal — „Knoten da" beweist nicht „Gerät benutzbar".

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

### Das Zeitbudget des Zusammenbaus

`img2pdf` und `ocrmypdf` liefen bis #11 ohne Zeitgrenze. Beide laufen über
`asyncio.to_thread` und sind unabbrechbar, also hätte ein einziger hängender
Kindprozess `JobManager._active` für immer gesetzt gelassen: jedes `start()`
antwortet `busy`, der blaue Knopf ist tot — und `watcher_health` meldet weiter
`ok`, weil das Gerät längst freigegeben ist. Ein stiller Dauerausfall, der
aussieht wie ein Gerätedefekt.

Die Grenze ist **gemessen, nicht gewählt** (2026-10-26, auf dem
Produktions-Mac, mit genau den Produktionsflags, über 2/8/20 Seiten):

| | je Seite | fest |
|---|---|---|
| `img2pdf` | ~1,6 s | 1,5 s |
| `ocrmypdf` | ~1,15 s | 5 s |

Budget: `ASSEMBLY_OVERHEAD + ASSEMBLY_PER_PAGE × Seiten` = **60 s + 20 s/Seite**,
**12,5× für `img2pdf` und 17× für `ocrmypdf`** — das Budget gilt je
Unterprozess, nicht über den ganzen Zusammenbau. (Gegen die Summe beider
Messwerte wären es 7×; das ist die Rechnung für ein Budget je Auftrag, und
ein solches ist NICHT gebaut — seit #13 läuft `assemble` nur noch einmal je
Dokument, die Grenze selbst gilt aber weiter je Unterprozess.) Der
Faktor ist absichtlich großzügig: sie soll ein *unbegrenztes* Warten begrenzen,
nicht eine langsame Maschine maßregeln — eine dichte Seite oder ein belasteter
Rechner darf seine Texterkennung nicht verlieren.

🛑 **Nicht `subprocess.run(timeout=...)`.** Das tötet nur das unmittelbare
Kind, und `ocrmypdf` ist ein Elternteil: ein Lauf hält gemessen sieben
tesseract-Nachkommen. Im gesunden Fall beenden die ihre Seite und gehen binnen
~4 s von selbst — aber der Fall, für den das Budget existiert, ist ein
*hängender* tesseract, und dort überlebt er den Tod des Elternteils unbegrenzt
und hält einen Kern und das Staging-Verzeichnis. `_run_bounded` gibt dem Kind
darum mit `start_new_session` eine eigene Prozessgruppe und schickt bei Ablauf
SIGKILL an die Gruppe. Kehrt die Nachlese danach nicht zurück, hält noch
jemand die Rohre — das ist eine `ERROR`-Zeile, keine stille Unterdrückung.

`ocrmypdf` ist der einzige Elternteil im Paket; `img2pdf`, `pdftotext` und
`scanimage` sind Einzelprozesse (die ersten beiden gegengemessen), deshalb
genügt ihnen das Töten des Kindes.

Die zwei Aufrufe scheitern unterschiedlich schwer, und das bleibt so:
`img2pdf`-Ablauf ist tödlich (`AssemblyError`) — ohne PDF gibt es nichts
abzuliefern. `ocrmypdf`-Ablauf ist nur eine weitere Art, auf die die
Texterkennung nicht zustande kam, und läuft deshalb in den *bestehenden*
Rückfallpfad: das un-OCRte PDF wird behalten, der Grund protokolliert. **Ein
Dokument geht nie verloren, weil die Texterkennung scheiterte.**

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```
