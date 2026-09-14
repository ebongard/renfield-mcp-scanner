"""MCP shell. Thin by design — every tool body lives in tools.py."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import Context, FastMCP

from . import tools as t
from .config import Config, load_config
from .auth import bearer_auth_middleware, is_loopback, require_tokens
from .jobs import JobEventNotifier, JobManager, JobStore
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
_jobs: JobManager | None = None


def _ctx() -> tuple[Config, Staging]:
    if _config is None or _staging is None:
        raise RuntimeError("scanner server not initialised")
    return _config, _staging


def _job_manager() -> JobManager:
    if _jobs is None:
        raise RuntimeError("scanner server not initialised")
    return _jobs


def _caller(ctx: Context | None) -> str:
    """The authenticated caller the Bearer middleware recorded on the request.

    Unauthenticated loopback serving has no middleware and so no caller; that is
    the "default" caller, the same name the legacy singular token maps to."""
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
        return getattr(request.state, "caller", None) or "default"
    except (AttributeError, ValueError, LookupError):
        return "default"


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
async def scan_document(ctx: Context, target: str = "", title: str = "") -> dict:
    """START scanning every sheet in the document feeder into one searchable PDF
    and filing it into a single Renfield instance.

    This returns IMMEDIATELY with a `job_id` — the scan itself runs in the
    background and takes from half a minute to several minutes. Tell the user the
    scan has started and that they will be notified when it is done. Do NOT say
    the document was scanned or filed, and do NOT call `scan_job_status` in a
    loop to wait for it: the outcome is reported automatically when it finishes.

    If a scan is already running this returns `busy` with that job's id — there
    is one feeder; do not retry.

    Pass `target` to name the destination instance when more than one is
    configured. With exactly one configured target it is chosen automatically.
    If the destination cannot be settled the scan is kept safely on the scanner
    host and waits for a routing decision — it is never filed into a guess.
    """
    return await _job_manager().start(caller=_caller(ctx), target=target, title=title)


@mcp.tool()
async def scan_job_status(ctx: Context, job_id: str) -> dict:
    """Report how a scan started with `scan_document` stands or ended — only when
    the user explicitly asks about it. Status is `running`, `done`, `unrouted`
    (waiting for a routing decision), `failed` or `interrupted`.

    In a finished result, `renfield_document_id` is the id IN RENFIELD. It is NOT
    a Paperless id: filing into Paperless runs asynchronously afterwards and gets
    its own separate id. Do not report it as a Paperless id, and do not claim the
    document is in Paperless yet.
    """
    return await _job_manager().status(job_id, caller=_caller(ctx))


@mcp.tool()
async def list_pending_scans() -> dict:
    """List scans held on the scanner host: awaiting a routing decision, or
    retained because a push or the scanner itself failed."""
    _, staging = _ctx()
    return {"ok": True, "pending": staging.pending()}


@mcp.tool()
async def route_scan(stage_id: str, target: str) -> dict:
    """Decide where a waiting scan belongs and file it there.

    Use this for scans reported by `list_pending_scans` that have no
    destination — either nothing named one, or the suggestion was not confident
    enough to file automatically. Nothing is ever filed on a guess, so this is
    how those are resolved.
    """
    config, staging = _ctx()
    return await t.route_scan(config, staging, assemble, stage_id, target)


@mcp.tool()
async def routing_audit(limit: int = 20) -> dict:
    """Show the recent routing decisions and the reason for each — which target
    a scan went to, by which layer (declared, separator sheet, classifier,
    human), and with what confidence."""
    _, staging = _ctx()
    return t.routing_audit(staging, limit)


@mcp.tool()
async def generate_separator_sheets() -> dict:
    """Render a printable separator sheet for every configured target.

    A sheet carries its destination ON THE PAPER, which is the only way an
    unattended stack can be routed. Sheets are generated from the configured
    targets, never written by hand, so a sheet and the configuration cannot
    drift apart — a drifted sheet fails silently, scanning fine while matching
    nothing.
    """
    from pathlib import Path

    from .sheets import render_all

    config, _ = _ctx()
    out_dir = Path(os.environ.get("SCANNER_SHEETS_DIR", "~/Scans")).expanduser()
    paths = render_all(config.targets, out_dir)
    return {"ok": True, "sheets": [{"target": t.id, "path": str(p)}
                                   for t, p in zip(config.targets, paths)]}


@mcp.tool()
async def retry_pending_scans(stage_id: str = "") -> dict:
    """Re-send scans that are waiting on this host because an earlier push
    failed — for example the backend was unreachable. Pass a stage_id to retry
    just one. Scans still awaiting a routing decision are skipped, since they
    have no destination yet."""
    config, staging = _ctx()
    return await t.retry_pending_scans(config, staging, stage_id=stage_id)


async def _serve() -> None:
    global _config, _staging, _jobs
    _config = load_config()
    _staging = Staging(_config.staging_dir)
    purged = _staging.purge_expired(_config.staging_retention_days)
    config, staging = _config, _staging
    store = JobStore(config.staging_dir)
    # Records left from before this start; from here on every job whose event
    # settles triggers the same sweep (JobManager._deliver_and_tidy).
    purged_jobs = await store.purge_settled(config.staging_retention_days)
    if purged_jobs:
        logger.info("purged %d settled job record(s)", purged_jobs)
    _jobs = JobManager(
        store,
        run_scan=lambda target, title: t.scan_document(
            config, staging, assemble, target=target, title=title),
        notifier=JobEventNotifier(config),
        retention_days=config.staging_retention_days,
    )
    # Close jobs a restart cut off and re-send events that never got through.
    await _jobs.recover()
    if config.mcp_tokens and not config.caller_targets:
        logger.warning("no SCANNER_CALLER_TARGET_* mapping — scans run, but no "
                       "instance is told when one finishes")
    # Fail-closed BEFORE binding: serving the LAN unauthenticated would let
    # anything on the network drive the scanner and file documents.
    tokens = require_tokens(_config.mcp_host, _config.mcp_tokens)

    logger.info("scanner MCP on %s:%s — %d target(s), %d pending, %d purged",
                _config.mcp_host, _config.mcp_port, len(_config.targets),
                len(_staging.pending()), purged)
    # Started here, NOT via FastMCP lifespan: lifespan is the per-MCP-session
    # hook, not ASGI startup, so anything registered there would only run while
    # an agent happens to be connected.
    # Build the app ONCE and serve THAT instance. FastMCP's
    # streamable_http_app() constructs a NEW Starlette app on every call, and
    # run_streamable_http_async() calls it again internally — so middleware added
    # to a separately-obtained app is silently DISCARDED and the endpoint serves
    # unauthenticated while looking configured. Found the hard way.
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware

    app = mcp.streamable_http_app()
    if tokens:
        app.add_middleware(BaseHTTPMiddleware, dispatch=bearer_auth_middleware(tokens))
        logger.info("MCP endpoint requires a Bearer token — callers: %s",
                    ", ".join(sorted(tokens)))
    elif is_loopback(_config.mcp_host):
        logger.info("MCP endpoint unauthenticated — bound to loopback only")

    await uvicorn.Server(
        uvicorn.Config(app, host=_config.mcp_host, port=_config.mcp_port,
                       log_level=os.environ.get("SCANNER_LOG_LEVEL", "info").lower())
    ).serve()


def main() -> None:
    asyncio.run(_serve())
