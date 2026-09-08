"""Agent-facing tool bodies. Deliberately free of any MCP import so they stay
unit-testable; server.py is a thin decorated shell over these."""

from __future__ import annotations

import logging
from pathlib import Path

from . import audit, classifier, sane, separator
from .config import Config, MissingTokenError
from .contract import StageAction
from .pusher import TargetPusher
from .router import Routing, route

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


async def _deliver(
    config: Config, staging, assemble, *, stage_dir, pages, target, title: str,
) -> dict:
    """Assemble one document's pages and push them to one target."""
    pdf_path = assemble(pages, stage_dir, config, title=title)
    try:
        token = target.token()
    except MissingTokenError as exc:
        staging.keep(stage_dir, reason="missing_token", detail=str(exc), target=target.id)
        return _err(str(exc))

    pusher = TargetPusher(target.base_url, token, config.push_timeout_seconds,
                          ca_bundle=target.ca_bundle)
    outcome = await pusher.push(
        pdf_path.name, pdf_path.read_bytes(),
        {"filename": pdf_path.name, "scan_profile_id": target.scan_profile_id,
         "title": title or "", "pages": len(pages), "source": "scanner"},
    )
    if outcome.action is StageAction.DISCARD:
        staging.discard(stage_dir)
    else:
        staging.keep(stage_dir, reason=outcome.status or outcome.detail,
                     detail=outcome.detail, target=target.id)

    return {"ok": outcome.action is StageAction.DISCARD, "routed": True,
            "target": target.id, "pages": len(pages), "status": outcome.status,
            # The id is NAMED for its system on purpose. It was returned as a
            # bare `document_id`, and the agent — surrounded by Paperless tooling
            # in the documents role — told the user "die Dokument-ID in Paperless
            # ist 426". It was the RENFIELD id, and the document was not in
            # Paperless at all yet.
            "renfield_document_id": outcome.document_id,
            "paperless_document_id": None,
            "paperless_note": ("Filing into Paperless happens asynchronously "
                               "afterwards and has its own separate id; it is "
                               "not known at scan time."),
            "fatal": outcome.fatal, "detail": outcome.detail,
            "stage_id": None if outcome.action is StageAction.DISCARD else stage_dir.name}


async def scan_document(
    config: Config, staging, assemble, *, target: str = "", title: str = "",
) -> dict:
    """Scan the feeder, correct it, split at separator sheets, and route.

    ``assemble`` builds the searchable PDF from the corrected pages; it is
    injected so the whole flow is testable without a scanner or ocrmypdf.
    """
    routing = route(config, declared=target)
    # Route BEFORE scanning when a destination was declared, so an unknown
    # target id costs nothing.
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

    segments = separator.segment(result.pages)
    used_separators = any(s.target_id for s in segments) or len(segments) > 1

    # SINGLE DOCUMENT — no separator sheets in the stack. Unchanged behaviour.
    if not used_separators:
        # L3 attaches HERE, after L1 and the n==1 short-circuit have both
        # declined, and only ever to raise an undecided scan to decided — never
        # to overrule a destination someone actually stated.
        if not routing.settled and config.classifier_url and config.classifier_model:
            pdf_probe = assemble(result.pages, stage_dir, config, title=title)
            text = classifier.extract_text(pdf_probe)
            guess = await classifier.classify(
                text, config.targets,
                url=config.classifier_url, model=config.classifier_model)
            if guess and guess.confidence >= config.route_auto_threshold:
                chosen = config.target_by_id(guess.target_id)
                if chosen is not None:
                    audit.record(staging.root, stage=stage_dir.name, layer="classified",
                                 target=guess.target_id, confidence=guess.confidence,
                                 evidence=guess.evidence)
                    routing = Routing(chosen, "classified", guess.confidence,
                                      f"classified: {guess.evidence}")
            elif guess:
                # Below the bar is the NORMAL outcome, not a failure. Record why,
                # so the review queue can show its reasoning to the human.
                audit.record(staging.root, stage=stage_dir.name, layer="undecided",
                             target=guess.target_id, confidence=guess.confidence,
                             evidence=guess.evidence,
                             threshold=config.route_auto_threshold)
                staging.keep(stage_dir, reason="unrouted",
                             detail=(f"classifier suggested {guess.target_id!r} at "
                                     f"{guess.confidence:.2f}, below the "
                                     f"{config.route_auto_threshold:.2f} threshold: "
                                     f"{guess.evidence}"))
                return {"ok": True, "routed": False, "pages": len(result.pages),
                        "stage_id": stage_dir.name,
                        "suggested_target": guess.target_id,
                        "confidence": guess.confidence, "evidence": guess.evidence,
                        "message": "Scan complete. The suggested destination was not "
                                   "confident enough to file automatically; it is "
                                   "waiting for a decision."}
        if not routing.settled:
            staging.keep(stage_dir, reason="unrouted", detail=routing.reason)
            return {"ok": True, "routed": False, "pages": len(result.pages),
                    "stage_id": stage_dir.name,
                    "message": "Scan complete but the destination is not settled; "
                               "it is waiting for a routing decision.",
                    "reason": routing.reason}
        audit.record(staging.root, stage=stage_dir.name, layer=routing.layer,
                     target=routing.target.id, confidence=routing.confidence)
        return await _deliver(config, staging, assemble, stage_dir=stage_dir,
                              pages=result.pages, target=routing.target, title=title)

    # MIXED STACK — a separator both marks the boundary AND names the target.
    # Splitting is unconditional; routing by sheet applies only when the
    # operator did not declare a destination. A declared intent is the more
    # explicit signal, so it wins — but a disagreement is logged, because
    # silently ignoring the paper in someone's hand is how trust is lost.
    documents, skipped = [], []
    for index, seg in enumerate(segments):
        if target and seg.target_id and seg.target_id != target:
            logger.warning(
                "separator sheet says %r but the request declared %r — honouring "
                "the declared target", seg.target_id, target)
        seg_target = routing.target if target else (
            config.target_by_id(seg.target_id) if seg.target_id else routing.target
        )
        if seg_target is None:
            # Pages before the first sheet, or a sheet naming an unknown target.
            skipped.append({"segment": index, "pages": len(seg.pages),
                            "reason": "unrouted",
                            "detail": f"separator target {seg.target_id!r} is not configured"
                                      if seg.target_id else
                                      "pages appeared before the first separator sheet"})
            continue
        sub = staging.new_stage()
        for page in seg.pages:
            page.rename(sub / "pages" / page.name)
        documents.append(await _deliver(config, staging, assemble, stage_dir=sub,
                                        pages=sorted((sub / "pages").glob("p*.png")),
                                        target=seg_target, title=title))

    if skipped:
        staging.keep(stage_dir, reason="unrouted",
                     detail=f"{len(skipped)} segment(s) without a destination")
    else:
        staging.discard(stage_dir)

    return {"ok": all(d.get("ok") for d in documents) and not skipped,
            "routed": True, "split": True,
            "documents": documents, "skipped": skipped,
            "segments": len(segments), "pages": len(result.pages)}


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
            "status": outcome.status,
            # Named for its system, same reason as in scan_document.
            "renfield_document_id": outcome.document_id,
            "resolved": outcome.action is StageAction.DISCARD,
            "detail": outcome.detail,
        })

    return {"ok": True, "retried": len(results), "results": results}


async def route_scan(config: Config, staging, assemble, stage_id: str, target: str) -> dict:
    """Resolve ONE waiting scan to a destination, by hand.

    This is the floor under the whole routing design: everything that could not
    be settled deterministically, and everything the classifier was not
    confident enough about, ends here — where a person decides. Nothing is ever
    filed on a guess, so this path must always exist and always work.
    """
    chosen = config.target_by_id(target)
    if chosen is None:
        return _err(f"unknown target {target!r}; known: {[t.id for t in config.targets]}")

    stage = staging.root / stage_id
    if not stage.exists():
        return _err(f"unknown stage {stage_id!r}")

    pdfs = sorted(stage.glob("*.pdf"))
    pages = sorted((stage / "pages").glob("p*.png"))
    if not pdfs and not pages:
        return _err(f"stage {stage_id!r} holds neither pages nor a PDF")
    if not pdfs:
        assemble(pages, stage, config)

    audit.record(staging.root, stage=stage_id, layer="human", target=target)
    return await _deliver(config, staging, assemble, stage_dir=stage,
                          pages=pages, target=chosen, title="")


def routing_audit(staging, limit: int = 20) -> dict:
    """The recent routing decisions and why they were made."""
    return {"ok": True, "decisions": audit.tail(staging.root, limit)}
