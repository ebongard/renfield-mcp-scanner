"""Image correction. Every constant here was MEASURED against real scans.

Six defects were found the hard way while building the Phase 0 script; this
module exists so they are fixed once, in code, instead of being rediscovered.
Full write-up: renfield/docs/design/scanner-ingest.md.

  1. SANE's default scan area is US LETTER (279.364mm), 17.7mm SHORTER than A4,
     so the footer of every A4 page is silently cut off — bank details, tax
     numbers, totals, signature lines. Handled in sane.py via --page-height.
  2. ocrmypdf's DEFAULT --optimize 1 transcodes scans to LOSSY JPEG (measured:
     a 2421x3299 RGB page came back at 3.2% of raw). We pass --optimize 0.
  3. Never deskew twice. The scanner deskews on raw sensor data; doing it again
     in ocrmypdf is a second resampling pass for no gain.
  4. Scanner-side --swdeskew CREATES skew on sparse back pages (measured ~34
     degrees). Off by default in sane.py.
  5. Raw colour is strongly blue: paper peak measured R226 G236 B253 on white
     paper (B-R = +26 across the page). Corrected below.
  6. Duplex show-through occupies the 200-224 band (7.08% of pixels), clear of
     real ink (0-79). Cleared below, in the SAME LUT as (5) — one resample.

And the trap found while fixing them: PIL does not carry PNG DPI across a
re-save, and img2pdf sizes the PDF page from exactly that. Losing it turned an
A4 page into a 26-inch page while the pixel data stayed perfect, so it is
invisible unless the page size is asserted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger("renfield-mcp-scanner.pipeline")

# Paper is neutral by definition, so each channel's paper peak is the anchor.
# Searching from mid-grey up: ink is always a minority of pixels on a document.
_PAPER_SEARCH_FLOOR = 128
# Below this, the page has no paper peak to anchor on (a photo, an inverted
# print). Rescaling would blow it out, so it is left untouched.
_DARK_PAGE_GUARD = 150


def paper_peaks(image: Image.Image) -> list[int]:
    """Per-channel modal value above mid-grey — the paper white of this scan."""
    return [
        max(range(_PAPER_SEARCH_FLOOR, 256), key=lambda v, h=c.histogram(): h[v])
        for c in image.split()
    ]


def correct(image: Image.Image, white_clip: float = 0.90) -> Image.Image:
    """Neutralise the colour cast and clear duplex show-through in ONE pass.

    Dividing each channel by its OWN paper peak removes the cast; the clip
    factor puts a level just below that peak at pure white, which erases
    show-through. Measured at 0.90: B-R +26.0 -> +2.6, show-through 7.08% ->
    1.93%, ink 5.59% -> 5.30% (that small change is antialiasing crisping, not
    lost text). Table rules, light logos and barcodes survive.

    Returns the image unchanged when there is no paper peak to anchor on.
    """
    rgb = image.convert("RGB")
    peaks = paper_peaks(rgb)
    if min(peaks) < _DARK_PAGE_GUARD:
        logger.info("dark page (peaks=%s) — left uncorrected", peaks)
        return rgb
    return Image.merge(
        "RGB",
        [
            c.point([min(255, int(v * 255.0 / (peak * white_clip))) for v in range(256)])
            for c, peak in zip(rgb.split(), peaks)
        ],
    )


def correct_file(path: Path, dpi: float, white_clip: float = 0.90) -> None:
    """Correct one scanned page IN PLACE, preserving DPI.

    The explicit dpi= on save is load-bearing: PIL drops the source pHYs, and
    img2pdf derives the PDF page size from image DPI. Without it an A4 page
    silently becomes a 1912x2631pt (26 inch) page while the pixels stay right.
    """
    with Image.open(path) as im:
        out = correct(im, white_clip=white_clip)
    out.save(path, dpi=(dpi, dpi))
