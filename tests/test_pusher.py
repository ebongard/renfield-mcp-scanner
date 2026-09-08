"""Outcome mapping. Every ambiguous result must KEEP the staged scan."""
import httpx
import pytest
from renfield_mcp_scanner.contract import StageAction
from renfield_mcp_scanner.pusher import TargetPusher


class _Client:
    """Stand-in for httpx.AsyncClient as used inside push()."""
    def __init__(self, result): self._result = result
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _resp(code, body=None, text="x"):
    return httpx.Response(code, json=body) if body is not None else httpx.Response(code, text=text)


async def _push(monkeypatch, result):
    monkeypatch.setattr("renfield_mcp_scanner.pusher.httpx.AsyncClient",
                        lambda **k: _Client(result))
    return await TargetPusher("http://b:8000", "tok").push("f.pdf", b"%PDF", {})


@pytest.mark.parametrize("status,action", [
    ("ingested", StageAction.DISCARD),
    ("duplicate", StageAction.DISCARD),
    ("retry", StageAction.KEEP),
    ("failed", StageAction.FAIL),
])
async def test_status_maps_to_action(monkeypatch, status, action):
    out = await _push(monkeypatch, _resp(200, {"status": status, "contract_version": "1"}))
    assert out.action is action and not out.fatal


async def test_transport_error_keeps(monkeypatch):
    out = await _push(monkeypatch, httpx.ConnectError("refused"))
    assert out.action is StageAction.KEEP and not out.fatal
    assert "transport_error" in out.detail


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_rejection_is_fatal_not_retryable(monkeypatch, code):
    # Retrying a bad token just hammers the backend forever.
    out = await _push(monkeypatch, _resp(code))
    assert out.fatal and out.action is StageAction.KEEP


async def test_503_is_retryable_not_fatal(monkeypatch):
    # Feature disabled or worker down: normal and temporary.
    out = await _push(monkeypatch, _resp(503))
    assert out.action is StageAction.KEEP and not out.fatal


async def test_200_with_unparseable_body_keeps(monkeypatch):
    out = await _push(monkeypatch, _resp(200, None, text="not json"))
    assert out.action is StageAction.KEEP and out.detail == "bad_json"


async def test_contract_skew_is_lenient(monkeypatch):
    # Skew warns but must never fail a real document.
    out = await _push(monkeypatch, _resp(200, {"status": "ingested", "contract_version": "99"}))
    assert out.action is StageAction.DISCARD


async def test_unknown_status_keeps(monkeypatch):
    out = await _push(monkeypatch, _resp(200, {"status": "teleported"}))
    assert out.action is StageAction.KEEP


def test_ca_bundle_is_used_for_verification():
    # A self-hosted backend often serves a private cert: curl may accept it from
    # the OS trust store while httpx does not (certifi). The fix is a bundle,
    # never verify=False.
    assert TargetPusher("http://b", "t", ca_bundle="/tmp/ca.pem")._verify == "/tmp/ca.pem"


def test_default_verification_when_no_bundle():
    assert TargetPusher("http://b", "t")._verify is True


def test_verification_is_never_disabled():
    # An unverified push would send documents to whatever answers the name.
    for bundle in ("", None):
        assert TargetPusher("http://b", "t", ca_bundle=bundle or "")._verify is not False
