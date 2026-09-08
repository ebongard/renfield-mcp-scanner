"""Agent-facing tool bodies. Deliberately free of any MCP import so they stay
unit-testable; server.py is a thin decorated shell over these."""

from __future__ import annotations

import logging
from pathlib import Path

from . import sane
from .config import Config, MissingTokenError
from .contract import StageAction
from .pusher import TargetPusher
from .router import route

logger = logging.getLogger("renfield-mcp-scanner.tools")


def _err(message: str) -> dict:
    """Agent-facing tools degrade with an error field; they do not raise."""
    return {"ok": False, "error": message}


async def list_scanners(config: Config) -> dict:
    try:
        device = await sane.find_device(config.device)
    except sane.ScannerError as exc:
        return _err(str(exc))
    return {
        "ok": True, "device": device,
        "settings": {"source": config.source, "mode": config.mode,
                     "resolution": config.resolution,
                     "page_height_mm": config.page_height_mm},
        "targets": [{"id": t.id, "label": t.label} for t in config.targets],
    }


async def scanner_status(config: Config) -> dict:
    """Real hardware state from the SANE sensor group — never inferred."""
    try:
        device = await sane.find_device(config.device)
        s = await sane.read_sensors(device)
    except sane.ScannerError as exc:
        return _err(str(exc))
    return {
        "ok": True, "device": device, "ready": s.ready,
        "paper_loaded": s.page_loaded, "cover_open": s.cover_open,
        "power_save": s.power_save, "double_feed": s.double_feed,
        "error_code": s.error_code,
    }


async def scan_document(
    config: Config, staging, assemble, *, target: str = "", title: str = "",
) -> dict:
    """Scan the feeder, correct it, and route it to exactly one instance.

    ``assemble`` builds the searchable PDF from the corrected pages; it is
    injected so the whole flow is testable without a scanner or ocrmypdf.
    """
    routing = route(config, declared=target)
    # Route BEFORE scanning when a destination was declared, so an unknown
    # target id costs nothing; an undecided routing still scans, because the
    # pages then wait in staging for a human rather than being refused.
    if routing.target is None and target:
        return _err(routing.reason)

    try:
        device = await sane.find_device(config.device)
    except sane.ScannerError as exc:
        return _err(str(exc))

    stage_dir = staging.new_stage()
    try:
        result = await sane.scan_batch(
            device, stage_dir / "pages",
            source=config.source, mode=config.mode, resolution=config.resolution,
            page_height_mm=config.page_height_mm,
        )
    except sane.ScannerError as exc:
        staging.discard(stage_dir)
        return _err(str(exc))

    if result.faulted:
        # Keep the pages: a partial stack is re-scannable only by re-feeding the
        # paper, and filing a silently short document is the worse outcome.
        staging.keep(stage_dir, reason="scanner_fault", detail=result.fault_detail)
        return _err(f"scanner faulted mid-stack after {len(result.pages)} page(s); "
                    f"pages kept at {stage_dir}. {result.fault_detail}")
    if not result.pages:
        staging.discard(stage_dir)
        return _err("no pages scanned — is the feeder loaded?")

    pdf_path = assemble(result.pages, stage_dir, config)

    if not routing.settled:
        staging.keep(stage_dir, reason="unrouted", detail=routing.reason)
        return {"ok": True, "routed": False, "pages": len(result.pages),
                "stage_id": stage_dir.name,
                "message": "Scan complete but the destination is not settled; "
                           "it is waiting for a routing decision.",
                "reason": routing.reason}

    try:
        token = routing.target.token()
    except MissingTokenError as exc:
        staging.keep(stage_dir, reason="missing_token", detail=str(exc))
        return _err(str(exc))

    pusher = TargetPusher(routing.target.base_url, token, config.push_timeout_seconds)
    outcome = await pusher.push(
        pdf_path.name, pdf_path.read_bytes(),
        {"filename": pdf_path.name, "scan_profile_id": routing.target.scan_profile_id,
         "title": title or "", "pages": len(result.pages), "source": "scanner"},
    )

    if outcome.action is StageAction.DISCARD:
        staging.discard(stage_dir)
    else:
        staging.keep(stage_dir, reason=outcome.status or outcome.detail,
                     detail=outcome.detail)

    return {"ok": outcome.action is StageAction.DISCARD, "routed": True,
            "target": routing.target.id, "routing_layer": routing.layer,
            "pages": len(result.pages), "status": outcome.status,
            "document_id": outcome.document_id,
            "fatal": outcome.fatal, "detail": outcome.detail,
            "stage_id": None if outcome.action is StageAction.DISCARD else stage_dir.name}
