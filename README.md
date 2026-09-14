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

## Tools

| Tool | Purpose |
|---|---|
| `list_scanners` | The attached scanner, its settings, configured targets |
| `scanner_status` | Real hardware state from the SANE sensors — never inferred |
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
