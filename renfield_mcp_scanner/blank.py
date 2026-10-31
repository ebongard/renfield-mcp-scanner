"""Drop blank BACK sides of a duplex scan — on evidence, never on a guess.

Why this exists (scan-quality audit 2026-10-31, last 30 batches of both
instances): 24 % (household) and 33 % (xidra) of all pages were near-blank back
sides of single-sided sheets fed in duplex. They are not harmless: 8 of 30
batches sat in Renfield's PDF-split review for up to ten days because the
lowest-confidence segment WAS a blank page, and when the split was confident a
blank page became a document of its own and was filed.

SANE's own ``--swskip`` cannot be tuned to fix it: it thresholds one number
(percentage of dark pixels), and a content page measured 7.62 % dark against
3.97 % for its reverse — too narrow a band to separate. So the decision here
uses THREE independent measurements, and a page is dropped only if all three
agree:

1. **Text** — tesseract reads no WORD (``is_word``: three letters in a row,
   or four digits in a row) with the same engine and language set ocrmypdf
   uses for the text layer. A character budget was the first rule and it
   dropped pages whose text OCR had READ ("bez. 12.10." is 10 characters).
2. **Luminance** — mean luminance at a coarse render (1/4 scale) of at least
   ``blank_min_luminance``. That is the audit's measure.
3. **Ink floor** — at FULL resolution, the area of ink pixels (dark OR
   coloured) inside the page margin stays below ``blank_ink_floor_mm2``. This
   is what keeps a lone signature, a stamp or a handwritten note: OCR does not
   read a signature, and a few strokes barely move a page mean. Isolated
   single-pixel sensor noise is removed first (3x3 median), so a noisy blank
   page is still blank.

🛑 The limit, stated once: ``pipeline.correct`` turns raw duplex show-through
(0.86 of the paper and lighter) into 242+; a mark FAINTER than that is
indistinguishable from show-through by brightness, and a pale mark whose colour
spread is below that of coloured show-through (~13-18) by colour. Such a mark
is dropped — that is why the default mode is ``off`` and ``measure`` exists.

All three are measured on the image AFTER ``pipeline.correct`` — the same
pixels that would go into the PDF.

What is never dropped, by construction and by explicit guard:

* a FRONT side, and anything from a simplex scan (only ``ADF Duplex`` has back
  sides);
* anything when the page count of a duplex scan is odd — then the parity
  (even page = back side) is not proven, so nothing is decided on it;
* the only page of a scan, and never so much that a scan would end up empty.

A back side is never the only page of a document: its front precedes it on the
same sheet. The one exception is a back side whose front is a SEPARATOR sheet —
there the "document" would consist of nothing but that blank back, which is
exactly the junk this module removes (``--swskip`` used to drop it too).

🛑 Parity needs SANE's skip OFF. ``--swskip`` drops a page inside the backend,
which renumbers every following page and turns backs into fronts. While this
module is active on a duplex scan, ``tools.scan_document`` passes
``--swskip 0``.

Evidence: every dropped page is logged with its measurements (no content), and
its image is moved to ``<stage>/dropped/`` with a JSON record. When the stage is
discarded after delivery, ``Staging.discard`` moves that directory to
``<root>/dropped-backs/<stage>/``, where the retention sweep removes it after
``staging_retention_days`` — a wrong drop is recoverable after delivery too.

Modes (``Config.blank_backs``): ``off`` (default) measures nothing; ``measure``
logs the verdicts (``measure``) and drops nothing; ``drop`` sets pages aside.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .pipeline import correct
from .staging import DROPPED_DIR

logger = logging.getLogger("renfield-mcp-scanner.blank")

#: The only SANE source that produces back sides.
DUPLEX_SOURCE = "ADF Duplex"

#: The evidence record next to the dropped pages (``DROPPED_DIR``, in the stage).
EVIDENCE_FILE = "blank-backs.json"

#: The coarse render the luminance is taken at: 1/4 scale (75 dpi at 300 dpi).
_COARSE_FACTOR = 4

#: Upper bound for one tesseract run on one page. A blank page takes about a
#: second; this only bounds a hang.
OCR_TIMEOUT = 120.0

_MM_PER_INCH = 25.4

#: Below this resolution the 3x3 median would erase real pen strokes.
_MEDIAN_MIN_DPI = 200

#: A WORD in OCR output: three letters in a row (any script, umlauts included)
#: or four digits in a row (a year, an amount, an account number). Tesseract's
#: noise on a blank or speckled page is punctuation, bars and one- or
#: two-character fragments ("|", "—", "ee", "i:"), which neither matches.
_WORD = re.compile(r"[^\W\d_]{3,}|\d{4,}")


def is_word(token: str) -> bool:
    return _WORD.search(token) is not None


def count_words(text: str) -> int:
    """How many tokens of *text* contain a word (see ``_WORD``)."""
    return sum(1 for token in text.split() if is_word(token))


@dataclass(frozen=True)
class BlankRules:
    """The thresholds, as configured. See ``Config`` for the defaults."""

    min_luminance: float
    ink_level: int
    ink_chroma: int
    ink_floor_mm2: float
    margin_mm: float
    white_clip: float
    dpi: float
    ocr_languages: str


@dataclass(frozen=True)
class PageVerdict:
    """What was measured on one back side, and why it was kept or dropped.

    Numbers only — never text. ``chars`` / ``words`` are ``None`` when OCR was
    not needed (an earlier gate already kept the page) or could not run.
    ``front`` is empty when the page's side is not known (``measure`` on a
    duplex scan with an odd page count)."""

    page: int            # 1-based position in the scan
    name: str            # file name, e.g. p0004.png
    front: str           # file name of the front of the same sheet
    mean_luminance: float | None
    ink_mm2: float | None
    chars: int | None
    drop: bool
    reason: str
    words: int | None = None


def measure_luminance(image: Image.Image) -> float:
    """Mean luminance at a coarse render (the audit's measure)."""
    gray = image.convert("L")
    w, h = gray.size
    small = gray.resize((max(1, w // _COARSE_FACTOR), max(1, h // _COARSE_FACTOR)),
                        Image.Resampling.BOX)
    return float(ImageStat.Stat(small).mean[0])


def measure_ink_mm2(image: Image.Image, rules: BlankRules) -> float:
    """Area of ink inside the page margin, at full resolution, in mm².

    A pixel is ink when it is DARK (luminance below ``ink_level``) or COLOURED
    (channel spread of at least ``ink_chroma``). The second half matters for a
    stamp: a pale violet or blue stamp can be lighter than the dark threshold,
    but after the white balance in ``pipeline.correct`` paper and show-through
    are neutral, so colour is a clean signal.

    Single-pixel sensor noise is removed with a 3x3 median before counting; a
    pen stroke at 300 dpi is 3-6 px wide and survives it. Below 200 dpi a fine
    stroke is only 1-2 px wide and the median would erase it, so there the
    filter is skipped (more is kept, never less).
    """
    rgb = image.convert("RGB")
    w, h = rgb.size
    margin = round(rules.margin_mm / _MM_PER_INCH * rules.dpi)
    if 2 * margin < w and 2 * margin < h and margin > 0:
        rgb = rgb.crop((margin, margin, w - margin, h - margin))
    r, g, b = rgb.split()
    dark = rgb.convert("L").point(lambda v: 255 if v < rules.ink_level else 0)
    spread = ImageChops.lighter(
        ImageChops.lighter(ImageChops.difference(r, g), ImageChops.difference(g, b)),
        ImageChops.difference(r, b),
    )
    coloured = spread.point(lambda v: 255 if v >= rules.ink_chroma else 0)
    ink = ImageChops.lighter(dark, coloured)
    if rules.dpi >= _MEDIAN_MIN_DPI:
        ink = ink.filter(ImageFilter.MedianFilter(3))
    pixels = ink.histogram()[255]
    mm_per_px = _MM_PER_INCH / rules.dpi
    return pixels * mm_per_px * mm_per_px


def ocr_probe(image: Image.Image, rules: BlankRules) -> Image.Image:
    """The image OCR reads: grey, with everything at or above ``ink_level`` —
    show-through — set to white.

    🛑 Measured 2026-10-31: on the corrected reverse of a text page (show-through
    at 242-246, invisible to the ink measure) tesseract's adaptive binarisation
    still found the mirrored lines and read 200 "words" of junk ("PIE", "it:")
    — every show-through back would have been kept by the text gate. The OCR
    gate is the backstop for SMALL text the area floor misses, so it reads
    exactly the pixels the ink measure would call dark.
    """
    level = rules.ink_level
    return image.convert("L").point(lambda v: v if v < level else 255)


def ocr_text(image: Image.Image, languages: str, workdir: Path | None = None) -> str | None:
    """The text tesseract reads on *image*; ``None`` on failure.

    The text is only COUNTED by the caller (characters, words) — it is never
    logged or stored.

    The image is the CORRECTED page, written to a temporary file inside
    *workdir* (the stage: private, 0700) so page content never leaves the
    staging area — the staged page itself stays untouched until
    ``pdf.assemble`` corrects it.
    """
    with tempfile.TemporaryDirectory(prefix=".blank-ocr-", dir=workdir) as tmp:
        path = Path(tmp) / "page.png"
        image.save(path)
        try:
            done = subprocess.run(
                ["tesseract", str(path), "stdout", "-l", languages],
                check=False, capture_output=True, timeout=OCR_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("blank-page OCR could not run: %s", exc)
            return None
    if done.returncode != 0:
        logger.warning("blank-page OCR failed (exit %d): %s", done.returncode,
                       done.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    return done.stdout.decode("utf-8", "replace")


def judge_back(path: Path, page: int, front: Path | None, rules: BlankRules,
               ocr: Callable[[Image.Image, str], str | None] | None = None) -> PageVerdict:
    """Decide one back side. Cheapest gate first; any doubt keeps the page.

    *front* is ``None`` when the page's side is unknown (``measure``)."""
    def verdict(drop: bool, reason: str, lum=None, ink=None, chars=None,
                words=None) -> PageVerdict:
        return PageVerdict(page=page, name=path.name,
                           front="" if front is None else front.name,
                           mean_luminance=None if lum is None else round(lum, 2),
                           ink_mm2=None if ink is None else round(ink, 2),
                           chars=chars, words=words, drop=drop, reason=reason)

    try:
        with Image.open(path) as raw:
            image = correct(raw, white_clip=rules.white_clip)
    except Exception as exc:  # noqa: BLE001 - an unreadable page is KEPT, never dropped
        logger.warning("blank check: %s unreadable, kept: %s", path.name, exc)
        return verdict(False, "unreadable")

    lum = measure_luminance(image)
    if lum < rules.min_luminance:
        return verdict(False, "luminance", lum)
    ink = measure_ink_mm2(image, rules)
    if ink >= rules.ink_floor_mm2:
        return verdict(False, "ink", lum, ink)
    probe = ocr_probe(image, rules)
    if ocr is None:
        text = ocr_text(probe, rules.ocr_languages, workdir=path.parent.parent)
    else:
        text = ocr(probe, rules.ocr_languages)
    if text is None:
        return verdict(False, "ocr_unavailable", lum, ink)
    chars = sum(1 for c in text if not c.isspace())
    words = count_words(text)
    if words:
        return verdict(False, "text", lum, ink, chars, words)
    return verdict(True, "blank", lum, ink, chars, words)


def select(pages: list[Path], *, source: str, rules: BlankRules,
           ocr: Callable[[Image.Image, str], str | None] | None = None,
           ) -> tuple[list[Path], list[PageVerdict]]:
    """Split *pages* into (kept pages, verdicts on every back side). Moves nothing.

    *pages* must be in scan order. The dropped pages are the verdicts with
    ``drop`` set. Returns the input unchanged, with no verdicts, whenever the
    decision cannot be made on proven ground (see the module docstring).
    """
    if source != DUPLEX_SOURCE or len(pages) < 2:
        return list(pages), []
    if len(pages) % 2:
        logger.warning("duplex scan with an odd page count (%d) — front/back parity "
                       "unproven, no blank back side is dropped", len(pages))
        return list(pages), []

    verdicts = [judge_back(pages[i], i + 1, pages[i - 1], rules, ocr=ocr)
                for i in range(1, len(pages), 2)]
    names = {v.name for v in verdicts if v.drop}
    kept = [p for p in pages if p.name not in names]
    if not kept:
        # Unreachable while fronts are never dropped — kept as the explicit
        # guard the design promises: a scan never becomes empty.
        return list(pages), []
    return kept, verdicts


def measure(pages: list[Path], *, source: str, rules: BlankRules,
            ocr: Callable[[Image.Image, str], str | None] | None = None,
            ) -> list[PageVerdict]:
    """``measure`` mode: the verdicts ``drop`` would reach. Moves nothing.

    In this mode SANE's ``--swskip`` is unchanged, so a duplex scan can come
    back with an odd page count (the backend skipped a page) and the parity is
    unproven. Then EVERY page is measured, with no side attached (``front``
    empty) — the calibration data is still complete, and nothing is claimed
    about which side a page was.
    """
    if source != DUPLEX_SOURCE or len(pages) < 2:
        return []
    if len(pages) % 2 == 0:
        return [judge_back(pages[i], i + 1, pages[i - 1], rules, ocr=ocr)
                for i in range(1, len(pages), 2)]
    return [judge_back(p, i + 1, None, rules, ocr=ocr) for i, p in enumerate(pages)]


def report_measured(stage: Path, verdicts: list[PageVerdict], *, job_id: str = "",
                    total: int = 0) -> None:
    """Log what ``drop`` would have done — numbers only, nothing moved."""
    if not verdicts:
        return
    would = [v for v in verdicts if v.drop]
    sided = all(v.front for v in verdicts)
    logger.info("job %s stage %s: blank check (measure only, nothing dropped%s) — %s; "
                "would drop %d of %d page(s)", job_id or "-", stage.name,
                "" if sided else ", odd page count: every page measured, side unknown",
                summarise(verdicts), len(would), total)
    for v in would:
        logger.info(
            "job %s stage %s: would drop %s (page %d of %d, front %s): "
            "mean_luminance=%.2f ink_mm2=%.2f chars=%d words=%d",
            job_id or "-", stage.name, v.name, v.page, total, v.front or "?",
            v.mean_luminance, v.ink_mm2, v.chars, v.words)


def summarise(verdicts: list[PageVerdict]) -> str:
    """One content-free line per scan: how many backs, why each was kept.

    The ink values of backs kept ONLY by the ink floor are listed, because they
    are the calibration data for ``blank_ink_floor_mm2``: a blank-looking page
    that the floor kept shows here with its measured area."""
    reasons: dict[str, int] = {}
    for v in verdicts:
        reasons[v.reason] = reasons.get(v.reason, 0) + 1
    by_ink = sorted(v.ink_mm2 for v in verdicts if v.reason == "ink" and v.ink_mm2 is not None)
    parts = ", ".join(f"{k}={n}" for k, n in sorted(reasons.items()))
    return (f"{len(verdicts)} back side(s): {parts or 'none'}"
            + (f"; ink_mm2 of backs kept by the ink floor: {by_ink[:20]}" if by_ink else ""))


def set_aside(stage: Path, pages_dir: Path, verdicts: list[PageVerdict], *,
              job_id: str = "", total: int = 0) -> list[PageVerdict]:
    """Move dropped pages out of ``pages/`` into ``<stage>/dropped/``, log each,
    and write the evidence record. Only measured numbers are logged. Returns
    the dropped verdicts."""
    dropped = [v for v in verdicts if v.drop]
    if verdicts:
        logger.info("job %s stage %s: blank check — %s", job_id or "-", stage.name,
                    summarise(verdicts))
    if not dropped:
        return []
    dest = stage / DROPPED_DIR
    dest.mkdir(exist_ok=True)
    for v in dropped:
        shutil.move(str(pages_dir / v.name), str(dest / v.name))
        logger.info(
            "job %s stage %s: dropped blank back side %s (page %d of %d, front %s): "
            "mean_luminance=%.2f ink_mm2=%.2f chars=%d words=%d",
            job_id or "-", stage.name, v.name, v.page, total, v.front,
            v.mean_luminance, v.ink_mm2, v.chars, v.words)
    _write_evidence(dest, dropped)
    return dropped


def move_evidence(src_stage: Path, dst_stage: Path, fronts: set[str]) -> list[PageVerdict]:
    """Hand the dropped back sides whose front is in *fronts* over to another
    stage — so the evidence lives exactly as long as the document it belongs to
    (a mixed stack delivers each segment from its own stage)."""
    src = src_stage / DROPPED_DIR
    records = _read_evidence(src)
    moving = [v for v in records if v.front in fronts]
    if not moving:
        return []
    dest = dst_stage / DROPPED_DIR
    dest.mkdir(exist_ok=True)
    for v in moving:
        shutil.move(str(src / v.name), str(dest / v.name))
    _write_evidence(dest, moving)
    _write_evidence(src, [v for v in records if v.front not in fronts])
    return moving


def _write_evidence(directory: Path, records: list[PageVerdict]) -> None:
    (directory / EVIDENCE_FILE).write_text(
        json.dumps([asdict(v) for v in records], indent=2))


def _read_evidence(directory: Path) -> list[PageVerdict]:
    try:
        raw = json.loads((directory / EVIDENCE_FILE).read_text())
    except (OSError, ValueError):
        return []
    return [PageVerdict(**r) for r in raw]
