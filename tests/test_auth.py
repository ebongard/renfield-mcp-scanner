"""MCP endpoint authentication.

This protects the OPPOSITE direction from a target's push token: it
authenticates Renfield TO this server. Without it, anything on the LAN can call
scan_document — driving physical hardware and filing documents into Renfield
under this server's own credential.
"""
import pytest
from renfield_mcp_scanner.auth import (
    MissingTokenError, bearer_auth_middleware, is_loopback, match_caller,
    require_tokens,
)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_recognised(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.228", "10.0.0.5", "scanner.local"])
def test_non_loopback_recognised(host):
    assert not is_loopback(host)


def test_refuses_to_serve_the_lan_without_a_token():
    # Fail-closed. Starting unauthenticated "just for now" is how an open
    # device-control endpoint becomes permanent.
    with pytest.raises(MissingTokenError, match="SCANNER_MCP_TOKEN"):
        require_tokens("0.0.0.0", {})
    with pytest.raises(MissingTokenError):
        require_tokens("192.168.1.228", {"a": "   "})


def test_loopback_without_a_token_is_allowed():
    assert require_tokens("127.0.0.1", {}) == {}


def test_tokens_are_stripped():
    assert require_tokens("0.0.0.0", {"a": "  secret  "}) == {"a": "secret"}


# --- one token per caller ----------------------------------------------------

def test_each_caller_is_identified_by_its_own_token():
    tokens = {"household": "aaa", "xidra": "bbb"}
    assert match_caller(tokens, "Bearer aaa") == "household"
    assert match_caller(tokens, "Bearer bbb") == "xidra"


def test_revoking_one_caller_leaves_the_others_working():
    # The whole point of per-caller tokens: one instance can be cut off without
    # disturbing the rest.
    remaining = {"xidra": "bbb"}
    assert match_caller(remaining, "Bearer aaa") is None
    assert match_caller(remaining, "Bearer bbb") == "xidra"


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer ", "Bearer wrong", "bearer aaa", "Basic aaa", "aaa",
])
def test_bad_headers_match_no_caller(header):
    assert match_caller({"household": "aaa"}, header) is None


class _Req:
    def __init__(self, header=None):
        self.headers = {"authorization": header} if header else {}
        self.client = type("C", (), {"host": "10.0.0.9"})()
        # The middleware records WHICH caller matched, for the audit trail.
        self.state = type("S", (), {})()


async def _call(header, expected="s3cr3t"):
    expected = {"caller": expected} if isinstance(expected, str) else expected
    called = {"n": 0}

    async def _next(_req):
        called["n"] += 1
        return "OK"

    mw = bearer_auth_middleware(expected)
    result = await mw(_Req(header), _next)
    return result, called["n"]


async def test_correct_token_passes_through():
    result, n = await _call("Bearer s3cr3t")
    assert result == "OK" and n == 1


async def test_the_matched_caller_is_recorded_on_the_request():
    # Needed for the audit trail: "who called" is as important as "was it valid".
    req = _Req("Bearer bbb")
    mw = bearer_auth_middleware({"household": "aaa", "xidra": "bbb"})

    async def _next(_r):
        return "OK"

    await mw(req, _next)
    assert req.state.caller == "xidra"


@pytest.mark.parametrize("header", [
    None, "", "s3cr3t", "Bearer", "Bearer ", "Bearer wrong",
    "bearer s3cr3t",          # scheme is case-sensitive here
    "Basic s3cr3t",
    "Bearer s3cr3t extra",
])
async def test_bad_or_missing_credentials_are_rejected(header):
    result, n = await _call(header)
    assert n == 0, "handler ran despite failed auth"
    assert result.status_code == 401


def test_middleware_is_applied_to_the_app_that_is_actually_served():
    """Regression: FastMCP builds a NEW Starlette app on every
    streamable_http_app() call, so middleware added to a separately-obtained
    app is silently discarded and the endpoint serves UNAUTHENTICATED while
    looking configured. The server must build the app once and serve THAT one."""
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("t")
    assert m.streamable_http_app() is not m.streamable_http_app(), (
        "FastMCP now caches the app; the guard below can be simplified"
    )
    app = m.streamable_http_app()
    before = len(app.user_middleware)
    from starlette.middleware.base import BaseHTTPMiddleware
    app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth_middleware("x"))
    assert len(app.user_middleware) == before + 1
    # ...and a freshly built app does NOT have it — the trap, asserted.
    assert len(m.streamable_http_app().user_middleware) == before
