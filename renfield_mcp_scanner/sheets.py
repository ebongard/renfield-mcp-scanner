"""Render printable separator sheets.

Generated FROM the target registry, never hand-made: a sheet whose payload has
drifted from the configuration is worse than no sheet, because it fails silently
— the stack scans, nothing matches, and the documents land unrouted with no
obvious cause.

Design constraints come from the scanner, not from taste: the sheet is scanned
at 300 dpi through an ADF, possibly slightly skewed, so the code is large, high
contrast, generously quiet-zoned, and placed in the upper area where a
sheet-fed page is most reliably captured.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import ScanTarget
from .separator import payload_for

logger = logging.getLogger("renfield-mcp-scanner.sheets")

# A4 at 300 dpi.
_W, _H = 2480, 3508
_MARGIN = 240


def render_sheet(target: ScanTarget, out_dir: Path, dpi: int = 300) -> Path:
    """Render one target's separator sheet as a PDF."""
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    payload = payload_for(target.id)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # survives a fold or a smudge
        box_size=20,
        border=4,  # quiet zone; zbar needs it and a scanner crop can eat a thin one
    )
    qr.add_data(payload)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    page = Image.new("RGB", (_W, _H), "white")
    draw = ImageDraw.Draw(page)

    side = min(1200, _W - 2 * _MARGIN)
    code = code.resize((side, side), Image.LANCZOS)
    page.paste(code, ((_W - side) // 2, _MARGIN + 260))

    def _font(size: int):
        for candidate in ("/System/Library/Fonts/Helvetica.ttc",
                          "/System/Library/Fonts/Supplemental/Arial.ttf"):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _centre(text: str, y: int, size: int):
        f = _font(size)
        w = draw.textbbox((0, 0), text, font=f)[2]
        draw.text(((_W - w) // 2, y), text, fill="black", font=f)

    _centre("TRENNBLATT", _MARGIN, 96)
    _centre(target.label, _MARGIN + 130, 130)
    _centre(payload, _MARGIN + 300 + side + 40, 54)
    _centre("Vor den Stapel legen. Diese Seite wird nicht mit eingelesen.",
            _MARGIN + 300 + side + 130, 48)
    _centre("Renfield Scanner — separator sheet", _H - _MARGIN, 40)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"Trennblatt-{target.id}.pdf"
    page.save(out, "PDF", resolution=float(dpi))
    logger.info("rendered separator sheet for %s -> %s", target.id, out)
    return out


def render_all(targets: list[ScanTarget], out_dir: Path) -> list[Path]:
    return [render_sheet(t, out_dir) for t in targets]
