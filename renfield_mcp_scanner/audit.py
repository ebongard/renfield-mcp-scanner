"""Append-only record of every routing decision.

Which instance a scan goes to is the one call that is NOT server-authoritative —
the router owns it, and no backend can check it. An audit trail is therefore the
only way to answer "why did this document end up there" after the fact, and the
only way a misroute is ever noticed at all.

Local, append-only, and never fatal: a failure to log must not stop a scan.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.audit")


def record(root: Path, **fields) -> None:
    """Append one decision. Swallows its own errors by design."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now().isoformat(), **fields},
                          ensure_ascii=False)
        with (root / "routing-audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:  # noqa: BLE001 - never break a scan to write a log
        logger.warning("could not write the routing audit: %s", exc)


def tail(root: Path, limit: int = 20) -> list[dict]:
    path = root / "routing-audit.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
