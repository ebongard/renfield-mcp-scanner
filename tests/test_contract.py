"""The contract's whole job is to never lose a scan on ambiguity."""
import pytest
from renfield_mcp_scanner.contract import IngestStatus, StageAction, action_for_status


@pytest.mark.parametrize("status,expected", [
    ("ingested", StageAction.DISCARD),
    ("duplicate", StageAction.DISCARD),
    ("retry", StageAction.KEEP),
    ("failed", StageAction.FAIL),
])
def test_known_statuses(status, expected):
    assert action_for_status(status) is expected


@pytest.mark.parametrize("status", ["", "a_future_status", "INGESTED", "null", "0"])
def test_unknown_status_keeps_the_pages(status):
    # Contract skew must never discard a scan: the paper would have to be fed
    # through again to recreate it.
    assert action_for_status(status) is StageAction.KEEP


def test_every_status_is_mapped():
    for s in IngestStatus:
        assert action_for_status(s.value) in StageAction
