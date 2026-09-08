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


def require_token(host: str, token: str) -> str:
    """Fail-closed: refuse to serve the LAN without a token.

    Starting unauthenticated 'just for now' is exactly how an open
    device-control endpoint becomes permanent, so this raises instead of
    warning.
    """
    tok = (token or "").strip()
    if not is_loopback(host) and not tok:
        raise MissingTokenError(
            f"refusing to bind {host} without SCANNER_MCP_TOKEN — an "
            "unauthenticated MCP endpoint on the network lets anyone drive the "
            "scanner and file documents. Set SCANNER_MCP_TOKEN, or bind 127.0.0.1."
        )
    return tok


def bearer_auth_middleware(expected: str):
    """Starlette middleware enforcing a Bearer token, compared constant-time."""

    async def middleware(request: Request, call_next):
        presented = request.headers.get("authorization", "")
        prefix = "Bearer "
        # compare_digest on the raw value: a plain == leaks length/prefix timing.
        ok = presented.startswith(prefix) and hmac.compare_digest(
            presented[len(prefix):], expected
        )
        if not ok:
            logger.warning(
                "rejected unauthenticated MCP request from %s",
                getattr(request.client, "host", "?"),
            )
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    return middleware
