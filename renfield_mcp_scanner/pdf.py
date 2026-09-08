"""Corrected pages -> one searchable PDF.

The two flags here are quality-critical and were established by measurement,
not preference (see pipeline.py for the full list):

  --optimize 0   ocrmypdf's DEFAULT (--optimize 1) transcodes large scans to
                 LOSSY JPEG. A real page came back as jpeg at 3.2% of raw. A
                 document scan is an archival master, not a web image. This
                 does not show up on small or mostly-white test pages, because
                 the optimizer only transcodes when it judges the saving
                 worthwhile — it must be verified on a real scan.
  no --deskew /  the scanner already handled geometry on raw sensor data.
  no --rotate-pages   Doing it again is a second resampling pass, and
                 rotate-pages' orientation detection fails quietly on sparse
                 pages and can rotate one the wrong way.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import Config
from .pipeline import correct_file

logger = logging.getLogger("renfield-mcp-scanner.pdf")


class AssemblyError(RuntimeError):
    pass


def assemble(pages: list[Path], stage: Path, config: Config) -> Path:
    """Correct each page in place, then build a searchable PDF."""
    for page in pages:
        correct_file(page, dpi=float(config.resolution), white_clip=config.white_clip)

    raw = stage / "scan-raw.pdf"
    out = stage / f"{stage.name}.pdf"
    try:
        subprocess.run(["img2pdf", "--output", str(raw), *[str(p) for p in pages]],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AssemblyError(f"img2pdf failed: {exc}") from exc

    ocr = subprocess.run(
        ["ocrmypdf", "-l", config.ocr_languages, "--skip-text", "--optimize", "0",
         "--quiet", str(raw), str(out)],
        capture_output=True,
    )
    if ocr.returncode != 0:
        # Do NOT discard the reason: a bad language, missing tessdata or an
        # unreadable page all land here, and "OCR failed" alone is undebuggable.
        logger.warning("OCR failed, keeping un-OCR'd PDF: %s",
                       ocr.stderr.decode("utf-8", "replace").strip()[:400])
        raw.replace(out)
    else:
        raw.unlink(missing_ok=True)
    return out
