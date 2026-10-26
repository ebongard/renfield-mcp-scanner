"""Agent-facing tool bodies. Deliberately free of any MCP import so they stay
unit-testable; server.py is a thin decorated shell over these."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import audit, button, classifier, sane, separator, usb
from .config import Config, MissingTokenError
from .contract import StageAction
from .pusher import TargetPusher
from .router import Routing, route

logger = logging.getLogger("renfield-mcp-scanner.tools")


def _err(message: str, code: str = "error") -> dict:
    """Agent-facing tools degrade with an error field; they do not raise.

    ``error_code`` is the stable, machine-readable half. A completion event
    carries the code, and the receiving instance renders its own fixed, localised
    text from it — free-form error text (exception strings, host paths) never
    becomes chat content on the other side."""
    return {"ok": False, "error": message, "error_code": code}


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


async def scanner_status(
    config: Config, *, liveness: button.WatcherLiveness | None = None,
) -> dict:
    """Real hardware state from the SANE sensor group — never inferred.

    Plus `button_watcher`: is the button watcher alive? That half is not
    hardware, and it is the reason this tool is worth probing. After #3 the
    watcher's death is LOUD (an ERROR line with a traceback) but it still only
    waits for someone to read the log — and on 2026-10-04 nobody read it for
    seven days. A functional probe calling this tool finds it by itself.

    🛑 The watcher verdict rides on the ERROR reply too. A sleeping or
    unplugged scanner is the NORMAL state on an operator workstation, so the
    early return below is the common case — and losing sight of the watcher
    exactly then would make the probe blind whenever it matters most.
    """
    verdict = (liveness or button.LIVENESS).report(enabled=config.button_watch)
    try:
        device = await sane.find_device(config.device)
        s = await sane.read_sensors(device)
    except sane.ScannerError as exc:
        return {**_err(str(exc)), "button_watcher": verdict}
    return {
        "ok": True, "device": device, "ready": s.ready,
        "paper_loaded": s.page_loaded, "cover_open": s.cover_open,
        "power_save": s.power_save, "double_feed": s.double_feed,
        "error_code": s.error_code, "button_watcher": verdict,
    }


async def watcher_health(
    config: Config, *,
    liveness: button.WatcherLiveness | None = None,
    scanner_node: Callable[[], Awaitable[usb.UsbNode | None]] = usb.scanner_node,
    device_held: Callable[[], bool] = sane.device_held,
    letzter_zugriff: Callable[[], dict | None] = sane.last_open,
) -> dict:
    """Befund für die funktionale Probe — OHNE das Gerät anzufassen.

    🛑 Warum nicht `scanner_status`: das fasst das Gerät an (`find_device` +
    `read_sensors`) und gibt bei fehlendem Gerät eine FEHLERANTWORT zurück. Ein
    ausgeschalteter Scanner ist auf einem Arbeitsplatzrechner der Normalfall,
    eine Probe darauf meldete also dauernd Alarm — genau deshalb hatte der
    Scanner bis 2026-10-21 gar keine Probe, und genau deshalb blieb der blaue
    Knopf neun Tage tot, ohne dass etwas es sagte.

    Dieses Werkzeug liest nur Dinge, die nichts kosten: die Zeitmarke des
    Wächters (im Speicher) und den USB-Knoten über `ioreg` (die Registratur des
    Kernels, öffnet das Gerät NICHT). Es kann den Scanner damit nicht
    verklemmen — nach dem 2026-10-21 keine Nebensache: dort war das Gerät neun
    Tage am Bus angemeldet und für SANE trotzdem unansprechbar, ohne dass eine
    einzige Gerätesuche gelaufen war.

    Die Entscheidung, Zeile für Zeile:

    ======================  ===============  ========  =======================
    Wächter                 `device_readable`  Knoten   Ergebnis
    ======================  ===============  ========  =======================
    `off` (absichtlich)     —                —         ok
    `starting`              —                —         ok
    `dead` / `crashloop`    —                —         **Befund**
    `alive`                 `False`          FEHLT     ok (Scanner aus)
    `alive`                 `False`          da        **Befund** (verklemmt)
    `alive`                 `False`          unbekannt ok (keine Auskunft)
    `alive`                 `True`/`None`    —         ok
    ======================  ===============  ========  =======================

    🛑 Die letzte Zeile ist die wichtige: liefert `ioreg` keine Auskunft (kein
    macOS, Zeitablauf, unlesbare Ausgabe), wird daraus KEIN Befund. Eine
    fehlende Messung darf nicht als Fehler gelesen werden — sonst tauscht man
    einen verschwiegenen Ausfall gegen einen erfundenen.
    """
    verdict = (liveness or button.LIVENESS).report(enabled=config.button_watch)
    node = None
    try:
        node = await scanner_node()
    except Exception as exc:  # noqa: BLE001 — Diagnose, nie tragend
        logger.debug("USB-Knoten nicht auswertbar: %s", exc)
    knoten = None if node is None else {
        "present": node.present,
        "session_id": node.session_id,
        "location_id": node.location_id,
    }
    nutzlast = {"button_watcher": verdict, "usb_node": knoten}

    if verdict["finding"]:
        # 🛑 Die Abbildung steht in `button.WATCHER_FINDING_CODES`, NICHT hier.
        # Eine zweite Verzweigung über dieselbe Zustandsmenge driftet: ein neu
        # eingeführter Befund-Zustand käme hier als „watcher_dead" heraus.
        # Fehlt ein Zustand in der Abbildung, ist der Code `watcher_finding` —
        # unspezifisch, aber ein ALARM, nie ein falsches Etikett und nie ein
        # verschwiegener Befund.
        code = button.WATCHER_FINDING_CODES.get(verdict["state"], "watcher_finding")
        return {**_err(verdict["detail"], code), **nutzlast}

    if device_held():
        # 🛑 Der häufigste Grund für „Gerät nicht lesbar" ist kein Defekt,
        # sondern ein laufender Scan: er hält den Einzug, und der Sensorabruf
        # daneben MUSS scheitern. Am 2026-10-21 gemessen — 4 Fehlschläge über
        # 31 s eines Knopf-Scans. Ohne diese Zeile meldet die Probe dafür
        # `device_wedged`, also einen Fehlalarm auf den normalsten Zustand
        # überhaupt. Eine Probe, die am Normalfall anschlägt, wird abgeschaltet.
        return {"ok": True, "device_held": True, **nutzlast}

    if (verdict["state"] == button.WATCHER_ALIVE
            and verdict["device_readable"] is False
            and node is not None and node.present):
        return {
            **_err(
                "Der Wächter lebt, der USB-Knoten ist vorhanden, aber das Gerät "
                "ist für SANE nicht ansprechbar — es ist verklemmt. Bekannter "
                "Rückweg: Scanner aus- und einschalten.",
                "device_wedged",
            ),
            **nutzlast,
        }

    # 🛑 Ohne Wächter gäbe es sonst GAR KEINE Verklemmungserkennung mehr.
    # `device_readable` setzt nur der Wächter; ist er aus — seit 2026-10-26 die
    # Vorgabe —, bleibt es `None` und der Zweig oben kann nie feuern. Das wäre
    # der Neun-Tage-Ausfall durch eine neue Tür: vom 22. bis 25.10. war das
    # Gerät praktisch durchgehend verklemmt, 640–669 Fehlzeilen am Tag.
    #
    # Der Ersatz kostet keine Öffnung: `sane.last_open()` hält fest, wie der
    # letzte ANGEFORDERTE Zugriff ausging. Das ist ausdrücklich keine
    # Live-Aussage, und die Meldung sagt das auch — sonst behauptete eine
    # Vergangenheitsform eine Gegenwart.
    letzter = letzter_zugriff()
    nutzlast["last_device_open"] = letzter
    if (verdict["state"] != button.WATCHER_ALIVE
            and letzter is not None and letzter["ok"] is False
            and node is not None and node.present):
        alter = max(0.0, time.time() - letzter["at"])
        return {
            **_err(
                "Der USB-Knoten ist vorhanden, aber der letzte Zugriff auf das "
                f"Gerät schlug fehl (vor {alter / 60:.0f} min): "
                f"{letzter['detail']}. Das spricht für eine Verklemmung. "
                "Bekannter Rückweg: Scanner aus- und einschalten. 🛑 Dies ist "
                "der letzte VERSUCH, keine Live-Messung — der Knopfwächter ist "
                "aus, es wird im Ruhezustand nichts geöffnet.",
                "device_wedged_last_attempt",
            ),
            **nutzlast,
        }

    return {"ok": True, **nutzlast}


async def _deliver(
    config: Config, staging, assemble, *, stage_dir, pages, target, title: str,
    pdf_path: Path | None = None,
) -> dict:
    """Assemble one document's pages and push them to one target.

    🛑 `pdf_path` ist eine PDF, die dieser Stapel SCHON hat — dann wird nicht
    erneut gebaut. ⚠️ Das kostet den OCR-WIEDERHOLUNGSVERSUCH: scheiterte die
    Texterkennung, traegt die behaltene Datei keine Textschicht, und sie wird
    jetzt unveraendert ausgeliefert statt neu gebaut. Bewusst so — die Ursachen
    (fehlende tessdata, unlesbare Seite, abgelaufenes Budget) aendern sich
    durch Wiederholen nicht, und ein zweiter Versuch kostet den vollen
    Durchlauf. Wer die Textschicht braucht, scannt neu. Im Review zu #13
    gefunden und hier festgehalten, damit es keine stille Aenderung bleibt. Ohne diesen Weg lief `assemble` zweimal über denselben
    Stapel, und das ist der TEUERSTE Schritt der ganzen Kette:

    * Der Klassifizierer-Pfad baut eine Sonden-PDF, um Text daraus zu lesen,
      und `_deliver` baute dieselbe PDF danach noch einmal.
    * `route_scan` baute sie bedingt („falls noch keine da ist") und liess
      `_deliver` sie danach bedingungslos erneut bauen — der bedingte Aufruf
      war damit vollständig sinnlos.

    Gemessen: `img2pdf` ~1,6 s/Seite, `ocrmypdf` ~1,15 s/Seite. Ein voller
    Einzug (duplex 100 Seiten) kostete also rund 69 Minuten doppelt. Und weil
    `output_name` minutengenau stempelt, hinterliessen zwei Läufe über eine
    Minutengrenze ZWEI PDFs im Stapel — eine davon eine Waise.
    """
    # Off the event loop: assemble shells out to img2pdf + ocrmypdf and blocks for
    # seconds per page. Run inline it froze the whole server — a list_tools from
    # Renfield went unanswered for 12s mid-scan, long enough for the backend's
    # refresh to declare the scanner dead and tear down the call in flight.
    if pdf_path is None:
        pdf_path = await asyncio.to_thread(assemble, pages, stage_dir, config, title=title)

    # 🛑 Eine LEERE Datei wird nie eingereicht — derselbe Satz wie „ein
    # Teilscan wird nie eingereicht", nur in der schlimmsten Form. Der Riegel
    # steht hier und nicht beim Erzeuger, weil er damit JEDE Quelle deckt:
    # eine 0-Byte-Zwischendatei aus einem gescheiterten `img2pdf`, eine
    # abgeschnittene Datei, ein kuenftiger Pfad, den es noch nicht gibt.
    #
    # Ohne ihn war die Folge Datenverlust: die leere Datei wird gepusht, die
    # Gegenseite antwortet `ingested`, und `discard` raeumt die Seiten weg —
    # das Papier muesste erneut durch den Einzug.
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        staging.keep(stage_dir, reason="empty_pdf",
                     detail=f"{pdf_path.name} ist leer — nicht eingereicht",
                     target=target.id)
        return _err(
            f"Der Zusammenbau ergab eine leere Datei ({pdf_path.name}). Der "
            "Stapel bleibt liegen, damit die Seiten nicht verloren gehen.",
            "empty_pdf",
        )
    try:
        token = target.token()
    except MissingTokenError as exc:
        staging.keep(stage_dir, reason="missing_token", detail=str(exc), target=target.id)
        return _err(str(exc), "missing_token")

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
            **({} if outcome.action is StageAction.DISCARD else {
                "error_code": "ingest_rejected"
                if outcome.fatal or outcome.action is StageAction.FAIL else "push_pending"}),
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
        return _err(routing.reason, "unknown_target")

    try:
        device = await sane.find_device(config.device)
    except sane.ScannerError as exc:
        return _err(str(exc), "device_unavailable")

    stage_dir = staging.new_stage()
    try:
        result = await sane.scan_batch(
            device, stage_dir / "pages",
            source=config.source, mode=config.mode, resolution=config.resolution,
            page_height_mm=config.page_height_mm,
        )
    except sane.ScannerError as exc:
        staging.discard(stage_dir)
        return _err(str(exc), "scan_error")

    if result.faulted:
        # Keep the pages: a partial stack is re-scannable only by re-feeding the
        # paper, and filing a silently short document is the worse outcome.
        staging.keep(stage_dir, reason="scanner_fault", detail=result.fault_detail)
        return _err(f"scanner faulted mid-stack after {len(result.pages)} page(s); "
                    f"pages kept at {stage_dir}. {result.fault_detail}", "scanner_fault")
    if not result.pages:
        staging.discard(stage_dir)
        return _err("no pages scanned — is the feeder loaded?", "no_pages")

    #: Eine im Klassifizierer-Zweig gebaute PDF, die die Lieferung
    #: wiederverwendet. `None` heisst „noch keine gebaut".
    pdf_probe = None
    # 🛑 Off the event loop, aus demselben Grund wie `assemble`: `segment`
    # oeffnet JEDE Seite mit PIL und laesst `pyzbar` einen Strichcode darauf
    # suchen. Gemessen am 2026-10-26 an echten Seiten (2550x3508): **411 ms je
    # Seite**. Ein halber Einzug (50 Seiten) fror die Schleife 20,5 s ein, ein
    # voller Duplex-Stapel (100 Seiten) 41 s — und genau das ist der Fehler,
    # den der Kommentar in `_deliver` schon beschreibt: ein `list_tools` vom
    # Backend blieb 12 s unbeantwortet, lang genug, dass dessen Auffrischung
    # den Scanner fuer tot erklaerte und den laufenden Aufruf abriss.
    #
    # ⚠️ Zwei bewusst offene Punkte, im Review zu #15 benannt:
    #
    # 1. Ein Abbruch raeumt den Thread NICHT ab. Vorher war `segment`
    #    ueberhaupt nicht abbrechbar; jetzt gibt es eine Abbruchstelle, der
    #    Thread laeuft aber zu Ende. Kein Datenschaden — `segment` liest nur.
    #    Ein Herunterfahren kann dadurch bis zu einem Stapeldurchlauf warten.
    #
    # 2. BILLIGER ginge es, und das ist inzwischen GEMESSEN (Review zu #15,
    #    synthetisches Blatt nach den Parametern aus `sheets.py`): bei 1/3
    #    Aufloesung faellt zbar von 117 ms auf 7 ms, erkannt wird bis 1/8.
    #    🛑 Aber es gibt einen BODEN von ~60 ms je Seite fuers Oeffnen des
    #    PNG — das Verkleinern ist also additiv zu diesem `to_thread`, kein
    #    Ersatz dafuer. Noch nicht umgesetzt, weil die Messung synthetisch war
    #    (kein Durchscheinen, keine Falte, kein Schmutz) und ein zu stark
    #    verkleinertes Blatt STILL nicht erkannt wird — das misroutet einen
    #    Stapel, ohne dass jemand es merkt. Der Entwurf, der die Frage
    #    entschaerft: 1/3 versuchen, und nur wenn nichts dekodiert, die volle
    #    Aufloesung nachschieben. Details und Tabelle im README.
    segments = await asyncio.to_thread(separator.segment, result.pages)
    used_separators = any(s.target_id for s in segments) or len(segments) > 1

    # SINGLE DOCUMENT — no separator sheets in the stack. Unchanged behaviour.
    if not used_separators:
        # L3 attaches HERE, after L1 and the n==1 short-circuit have both
        # declined, and only ever to raise an undecided scan to decided — never
        # to overrule a destination someone actually stated.
        if not routing.settled and config.classifier_url and config.classifier_model:
            # Dieselben Seiten, derselbe Stapel, derselbe Titel wie unten in
            # der Lieferung — die Sonden-PDF IST die Lieferung, sie wird also
            # weitergegeben statt weggeworfen.
            pdf_probe = await asyncio.to_thread(
                assemble, result.pages, stage_dir, config, title=title)
            text = await asyncio.to_thread(classifier.extract_text, pdf_probe)
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
                              pages=result.pages, target=routing.target, title=title,
                              pdf_path=pdf_probe)

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
    audit.record(staging.root, stage=stage_id, layer="human", target=target)
    # 🛑 Hier stand ein `if not pdfs: assemble(...)` davor, waehrend `_deliver`
    # danach bedingungslos erneut baute — der bedingte Aufruf war wirkungslos
    # und kostete einen vollen OCR-Durchlauf. Jetzt baut `_deliver` genau
    # einmal, oder gar nicht, wenn der Stapel schon eine PDF traegt (der
    # Normalfall nach einem Klassifizierer-Lauf, der unrouted endete).
    return await _deliver(config, staging, assemble, stage_dir=stage,
                          pages=pages, target=chosen, title="",
                          pdf_path=pdfs[0] if pdfs else None)


def routing_audit(staging, limit: int = 20) -> dict:
    """The recent routing decisions and why they were made."""
    return {"ok": True, "decisions": audit.tail(staging.root, limit)}
