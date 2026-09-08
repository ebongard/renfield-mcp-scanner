"""Bearer authentication for the MCP endpoint itself.

Distinct from the folder-ingest token, and deliberately so — they protect
opposite directions:

  * ``SCANNER_TOKEN_*``  authenticates THIS server TO Renfield (push).
  * ``SCANNER_MCP_TOKEN`` authenticates RENFIELD TO THIS SERVER (tool calls).

Without the second one, anything on the LAN could call ``scan_document`` — which
drives physical hardware and files documents into Renfield under this server's
own push credential — or read ``list_pending_scans`` for metadata about staged
documents. The Renfield MCP client already supports this: a server stanza
carrying ``auth_token_env`` makes it send ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("renfield-mcp-scanner.auth")


class MissingTokenError(RuntimeError):
    """Bound to a non-loopback address with no token configured."""


def is_loopback(host: str) -> bool:
    h = (host or "").strip()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def require_tokens(host: str, tokens: dict[str, str]) -> dict[str, str]:
    """Fail-closed: refuse to serve the LAN without at least one token.

    Starting unauthenticated 'just for now' is exactly how an open
    device-control endpoint becomes permanent, so this raises instead of
    warning.
    """
    clean = {name: t.strip() for name, t in (tokens or {}).items() if (t or "").strip()}
    if not is_loopback(host) and not clean:
        raise MissingTokenError(
            f"refusing to bind {host} without a caller token — an "
            "unauthenticated MCP endpoint on the network lets anyone drive the "
            "scanner and file documents. Set SCANNER_MCP_TOKEN_<CALLER>, or "
            "bind 127.0.0.1."
        )
    return clean


def match_caller(tokens: dict[str, str], presented: str) -> str | None:
    """Which configured caller presented this Bearer header, if any.

    ONE token per caller, so an instance can be revoked without disturbing the
    others — the same reason the push direction has one credential per target.
    Every candidate is compared with compare_digest: a plain == leaks length and
    prefix through timing, and an early exit would leak which caller matched.
    """
    prefix = "Bearer "
    if not presented.startswith(prefix):
        return None
    offered = presented[len(prefix):]
    found = None
    for name, token in tokens.items():
        if hmac.compare_digest(offered, token):
            found = found or name
    return found


def bearer_auth_middleware(tokens: dict[str, str]):
    """Starlette middleware enforcing a per-caller Bearer token."""

    async def middleware(request: Request, call_next):
        caller = match_caller(tokens, request.headers.get("authorization", ""))
        if caller is None:
            logger.warning(
                "rejected unauthenticated MCP request from %s",
                getattr(request.client, "host", "?"),
            )
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        request.state.caller = caller
        return await call_next(request)

    return middleware
