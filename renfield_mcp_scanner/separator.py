"""Barcode separator sheets — Phase 2.

A separator sheet is the only routing layer that carries its destination ON THE
PAPER. That makes it the one mechanism that works for an unattended stack: a
button press or a bare "scan this" carries no intent, while a cover sheet does.
It is also the only layer that marks a document BOUNDARY and a DESTINATION with
one mark, which is what lets a mixed stack be split and routed in a single pass.

The payload is namespaced and versioned on purpose:

    RFSEP1:<target_id>

Real documents carry barcodes of their own — a GiroCode on an invoice, a
tracking code on a parcel notice, an archive stamp. Encoding a bare label like
"HAUSHALT" would let any of those masquerade as a separator and silently split a
document in half, or route it somewhere else. The prefix makes a false positive
require deliberate effort rather than coincidence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.separator")

PAYLOAD_PREFIX = "RFSEP1"
_PAYLOAD_RE = re.compile(rf"^{PAYLOAD_PREFIX}:([a-z0-9][a-z0-9-]{{0,63}})$")


def payload_for(target_id: str) -> str:
    """The exact string encoded on a target's sheet."""
    return f"{PAYLOAD_PREFIX}:{target_id}"


def target_from_payload(payload: str) -> str | None:
    """Extract the target id, or None when this is not one of our separators.

    Anything that is not an exact RFSEP1 match is treated as ordinary document
    content — never as a boundary.
    """
    m = _PAYLOAD_RE.match((payload or "").strip())
    return m.group(1) if m else None


@dataclass(frozen=True)
class Segment:
    """One document within a stack: its pages, and where it belongs.

    ``target_id`` is None for pages that appeared BEFORE any separator — a stack
    that simply was not prefixed with a sheet. Those are not guessed at; they
    fall back to the caller's declared intent, or to the review queue.
    """

    target_id: str | None
    pages: list[Path]


def decode_page(path: Path) -> str | None:
    """Return the target id if this page IS a separator sheet, else None."""
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:  # pragma: no cover - optional extra
        logger.warning("pyzbar/zbar unavailable — separator sheets disabled")
        return None
    try:
        with Image.open(path) as im:
            for found in decode(im.convert("RGB")):
                target = target_from_payload(found.data.decode("utf-8", "ignore"))
                if target:
                    return target
    except Exception as exc:  # noqa: BLE001 - a bad page must not kill the scan
        logger.warning("separator decode failed for %s: %s", path.name, exc)
    return None


def segment(pages: list[Path]) -> list[Segment]:
    """Split a scanned stack at its separator sheets.

    The separator pages are CONSUMED — they are routing marks, not content, and
    must not reach the document. A separator with no pages after it (a trailing
    sheet, or two in a row) yields no segment rather than an empty document.
    """
    segments: list[Segment] = []
    current: list[Path] = []
    current_target: str | None = None

    for page in pages:
        found = decode_page(page)
        if found is None:
            current.append(page)
            continue
        # Boundary: close what came before, then start a new document.
        if current:
            segments.append(Segment(current_target, current))
        current, current_target = [], found

    if current:
        segments.append(Segment(current_target, current))
    return segments
