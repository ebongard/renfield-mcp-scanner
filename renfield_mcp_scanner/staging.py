"""Durable staging for scans that have not (yet) reached an instance.

This directory is the reason an unrouted scan never has to touch a database:
it holds pages and the assembled PDF on the ROUTER's disk until a destination
is settled, so nothing transits an instance that may turn out to be the wrong
one.

It is also the crash/sleep survival story. The operator host is a workstation:
it sleeps, it reboots, the backend may be down when a scan finishes. A staged
scan survives all three and is retried or resolved later. Pages are only ever
discarded after an acknowledged terminal outcome.

Contents are private documents. The directory is created 0700 and lives outside
any repository.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.staging")

_META = "stage.json"


class Staging:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def new_stage(self) -> Path:
        # The random suffix is not cosmetic: a timestamp alone (even to the
        # millisecond) can collide for two scans started in the same instant,
        # and a collision means two documents sharing one directory and
        # overwriting each other's pages.
        stage = self.root / f"scan-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        (stage / "pages").mkdir(parents=True, exist_ok=True)
        stage.chmod(0o700)
        return stage

    def keep(self, stage: Path, *, reason: str, detail: str = "",
             target: str | None = None) -> None:
        """Retain a stage with the reason it is still here.

        ``target`` is load-bearing for recovery: without it a retry cannot know
        where the scan was destined, and KEEP degrades into "kept forever".
        A scan kept because it was UNROUTED has no target by definition — that
        one needs a human decision, not a retry.
        """
        (stage / _META).write_text(json.dumps(
            {"reason": reason, "detail": detail, "target": target,
             "at": datetime.now().isoformat()},
            indent=2))
        logger.info("stage %s kept (%s)", stage.name, reason)

    def discard(self, stage: Path) -> None:
        """Remove a stage. Only ever after an acknowledged terminal outcome."""
        shutil.rmtree(stage, ignore_errors=True)
        logger.info("stage %s discarded", stage.name)

    def pending(self) -> list[dict]:
        out = []
        for stage in sorted(self.root.glob("scan-*")):
            meta_file = stage / _META
            meta = {}
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                except ValueError:
                    meta = {"reason": "unreadable_metadata"}
            out.append({
                "stage_id": stage.name,
                "pages": len(list((stage / "pages").glob("p*.png"))),
                "has_pdf": any(stage.glob("*.pdf")),
                **meta,
            })
        return out

    def purge_expired(self, retention_days: int) -> int:
        """Retention sweep. Private documents do not sit here forever."""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        for stage in self.root.glob("scan-*"):
            if datetime.fromtimestamp(stage.stat().st_mtime) < cutoff:
                shutil.rmtree(stage, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("purged %d stage(s) older than %dd", removed, retention_days)
        return removed
