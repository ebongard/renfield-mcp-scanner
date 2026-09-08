"""The cross-repo ingest seam. Mirrors the sibling ingest MCPs verbatim.

This module is the CONTRACT with the Renfield backend's folder-ingest route.
Changing anything here requires a matching change on the backend side AND a
version bump on both — the backend pins these in
``tests/backend/test_folder_ingest_route.py``.

The load-bearing rule is `move_action_for`: an outcome we do not recognise
resolves to KEEP, never to a destructive action. Contract skew must never lose
a scan.
"""

from enum import Enum

SCANNER_INGEST_CONTRACT_VERSION = "1"
CONTRACT_HEADER = "X-Folder-Ingest-Contract"

INGEST_PATH = "/api/folder-ingest/document"
HEALTH_PATH = "/api/folder-ingest/health"


class IngestStatus(str, Enum):
    """The backend's 4-state reply. All four arrive as HTTP 200."""

    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    RETRY = "retry"
    FAILED = "failed"


class StageAction(str, Enum):
    """What the router does with a staged scan after a push attempt.

    DISCARD only ever follows a terminal, acknowledged outcome. Everything
    ambiguous resolves to KEEP so the pages survive for another attempt — a
    scanned document cannot be re-created without the paper going back through
    the feeder, so losing one costs the operator a physical re-scan.
    """

    DISCARD = "discard"
    KEEP = "keep"
    FAIL = "fail"


_ACTION_BY_STATUS = {
    IngestStatus.INGESTED: StageAction.DISCARD,
    IngestStatus.DUPLICATE: StageAction.DISCARD,
    IngestStatus.RETRY: StageAction.KEEP,
    IngestStatus.FAILED: StageAction.FAIL,
}


def action_for_status(status: str) -> StageAction:
    """Map a backend status to a staging action. Unknown status -> KEEP.

    Deliberately lenient: a backend that grows a fifth status must not cause an
    older scanner build to delete pages it does not understand.
    """
    try:
        return _ACTION_BY_STATUS[IngestStatus(status)]
    except (ValueError, KeyError):
        return StageAction.KEEP
