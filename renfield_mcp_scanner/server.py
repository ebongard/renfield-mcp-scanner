"""MCP shell. Thin by design — every tool body lives in tools.py."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import tools as t
from .config import Config, load_config
from .pdf import assemble
from .staging import Staging

logging.basicConfig(
    level=os.environ.get("SCANNER_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,   # stderr: stdout must stay clean for MCP stdio
)
logger = logging.getLogger("renfield-mcp-scanner")

mcp = FastMCP(
    "renfield-mcp-scanner",
    host=os.environ.get("SCANNER_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("SCANNER_MCP_PORT", "9093")),
)

_config: Config | None = None
_staging: Staging | None = None


def _ctx() -> tuple[Config, Staging]:
    if _config is None or _staging is None:
        raise RuntimeError("scanner server not initialised")
    return _config, _staging


@mcp.tool()
async def list_scanners() -> dict:
    """List the attached document scanner, its scan settings and the configured
    routing targets."""
    config, _ = _ctx()
    return await t.list_scanners(config)


@mcp.tool()
async def scanner_status() -> dict:
    """Report the scanner's real hardware state: ready, paper loaded, cover
    open, power save, double feed, error code."""
    config, _ = _ctx()
    return await t.scanner_status(config)


@mcp.tool()
async def scan_document(target: str = "", title: str = "") -> dict:
    """Scan every sheet in the document feeder into one searchable PDF and file
    it into a single Renfield instance.

    Pass `target` to name the destination instance when more than one is
    configured. With exactly one configured target it is chosen automatically.
    If the destination cannot be settled the scan is kept safely on the scanner
    host and waits for a routing decision — it is never filed into a guess.
    """
    config, staging = _ctx()
    return await t.scan_document(config, staging, assemble, target=target, title=title)


@mcp.tool()
async def list_pending_scans() -> dict:
    """List scans held on the scanner host: awaiting a routing decision, or
    retained because a push or the scanner itself failed."""
    _, staging = _ctx()
    return {"ok": True, "pending": staging.pending()}


async def _serve() -> None:
    global _config, _staging
    _config = load_config()
    _staging = Staging(_config.staging_dir)
    purged = _staging.purge_expired(_config.staging_retention_days)
    logger.info("scanner MCP on %s:%s — %d target(s), %d pending, %d purged",
                _config.mcp_host, _config.mcp_port, len(_config.targets),
                len(_staging.pending()), purged)
    # Started here, NOT via FastMCP lifespan: lifespan is the per-MCP-session
    # hook, not ASGI startup, so anything registered there would only run while
    # an agent happens to be connected.
    await mcp.run_streamable_http_async()


def main() -> None:
    asyncio.run(_serve())
