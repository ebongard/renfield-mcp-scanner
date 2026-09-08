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

    pdf_path = assemble(result.pages, stage_dir, config, title=title)

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
        staging.keep(stage_dir, reason="missing_token", detail=str(exc),
                     target=routing.target.id)
        return _err(str(exc))

    pusher = TargetPusher(routing.target.base_url, token, config.push_timeout_seconds,
                          ca_bundle=routing.target.ca_bundle)
    outcome = await pusher.push(
        pdf_path.name, pdf_path.read_bytes(),
        {"filename": pdf_path.name, "scan_profile_id": routing.target.scan_profile_id,
         "title": title or "", "pages": len(result.pages), "source": "scanner"},
    )

    if outcome.action is StageAction.DISCARD:
        staging.discard(stage_dir)
    else:
        staging.keep(stage_dir, reason=outcome.status or outcome.detail,
                     detail=outcome.detail, target=routing.target.id)

    return {"ok": outcome.action is StageAction.DISCARD, "routed": True,
            "target": routing.target.id, "routing_layer": routing.layer,
            "pages": len(result.pages), "status": outcome.status,
            "document_id": outcome.document_id,
            "fatal": outcome.fatal, "detail": outcome.detail,
            "stage_id": None if outcome.action is StageAction.DISCARD else stage_dir.name}


async def retry_pending_scans(config: Config, staging, stage_id: str = "") -> dict:
    """Re-push scans that were kept because a push failed.

    Staging keeps a scan on ANY ambiguous outcome — a transport error, a 503, an
    unparseable reply — which is what stops a failed push costing a re-feed of
    the paper. But keeping without a retry path just means "kept forever", so
    this is the recovery leg of that contract.

    Only scans with a recorded target are retried. One kept because it was
    UNROUTED has no destination by definition and needs a human decision, not a
    retry: pushing it somewhere would be the guess the whole design refuses.
    """
    results = []
    for entry in staging.pending():
        if stage_id and entry["stage_id"] != stage_id:
            continue
        stage = staging.root / entry["stage_id"]
        target_id = entry.get("target")
        if not target_id:
            results.append({"stage_id": entry["stage_id"], "skipped": "unrouted",
                            "detail": "no destination recorded — needs a routing decision"})
            continue
        target = config.target_by_id(target_id)
        if target is None:
            results.append({"stage_id": entry["stage_id"], "skipped": "unknown_target",
                            "detail": f"target {target_id!r} is no longer configured"})
            continue
        pdfs = sorted(stage.glob("*.pdf"))
        if not pdfs:
            results.append({"stage_id": entry["stage_id"], "skipped": "no_pdf"})
            continue

        try:
            token = target.token()
        except MissingTokenError as exc:
            results.append({"stage_id": entry["stage_id"], "skipped": "missing_token",
                            "detail": str(exc)})
            continue

        pdf = pdfs[0]
        pusher = TargetPusher(target.base_url, token, config.push_timeout_seconds,
                              ca_bundle=target.ca_bundle)
        outcome = await pusher.push(
            pdf.name, pdf.read_bytes(),
            {"filename": pdf.name, "scan_profile_id": target.scan_profile_id,
             "pages": entry.get("pages", 0), "source": "scanner"},
        )
        if outcome.action is StageAction.DISCARD:
            staging.discard(stage)
        else:
            staging.keep(stage, reason=outcome.status or outcome.detail,
                         detail=outcome.detail, target=target_id)
        results.append({
            "stage_id": entry["stage_id"], "target": target_id,
            "status": outcome.status, "document_id": outcome.document_id,
            "resolved": outcome.action is StageAction.DISCARD,
            "detail": outcome.detail,
        })

    return {"ok": True, "retried": len(results), "results": results}
