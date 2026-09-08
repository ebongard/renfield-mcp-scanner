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

Phase 1: declared-intent routing (`scan_document(target=...)`), the `n = 1`
short-circuit, real hardware status, durable staging. Phase 2 (barcode separator
sheets) and Phase 3 (content classification behind a confidence gate, plus the
review floor) attach at marked points in `router.py`.

## Tools

| Tool | Purpose |
|---|---|
| `list_scanners` | The attached scanner, its settings, configured targets |
| `scanner_status` | Real hardware state from the SANE sensors — never inferred |
| `scan_document` | Scan the feeder, correct, route, push |
| `list_pending_scans` | Scans held here awaiting a decision or a retry |

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
