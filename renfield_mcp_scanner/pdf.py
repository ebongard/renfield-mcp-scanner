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

import contextlib
import logging
import os
import re
import signal
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path

from .config import Config
from .pipeline import correct_file

logger = logging.getLogger("renfield-mcp-scanner.pdf")


class AssemblyError(RuntimeError):
    pass


# Gemessen am 2026-10-26 auf dem Produktions-Mac, mit genau den Flags unten
# (deu+eng, --optimize 0, --skip-text): img2pdf ~1,6 s/Seite + 1,5 s fest,
# ocrmypdf ~1,15 s/Seite + 5 s fest. Das Budget laesst rund das Siebenfache je
# Seite stehen, denn es soll ein UNBEGRENZTES Warten begrenzen, nicht eine
# langsame Maschine maßregeln: eine dichte Seite oder ein belasteter Rechner
# darf seine Texterkennung nicht verlieren.
#
# 🛑 Warum es ueberhaupt eine Grenze braucht: beide Aufrufe laufen ueber
# `asyncio.to_thread` aus `tools.scan_document` und sind damit nicht
# abbrechbar. Haengt einer, bleibt `JobManager._active` fuer immer gesetzt,
# jedes weitere `start()` antwortet `busy` — der blaue Knopf ist tot, waehrend
# `watcher_health` weiter `ok` meldet. Ein stiller Dauerausfall.
ASSEMBLY_OVERHEAD = 60.0
ASSEMBLY_PER_PAGE = 20.0

# Nach dem Toeten noch kurz auf die Rohre warten, damit kein Zombie bleibt.
_REAP_GRACE = 5.0


def assembly_budget(pages: int) -> float:
    """Wanduhr-Budget fuer einen Zusammenbauschritt ueber *pages* Seiten."""
    return ASSEMBLY_OVERHEAD + ASSEMBLY_PER_PAGE * max(pages, 1)


def _run_bounded(argv: list[str], budget: float) -> subprocess.CompletedProcess[bytes]:
    """Fuehrt *argv* mit Zeitbudget aus und nimmt bei Ablauf die GANZE Sippe mit.

    🛑 Nicht `subprocess.run(timeout=...)`. Das toetet nur das unmittelbare
    Kind, und ocrmypdf ist ein Elternteil: ein Lauf haelt bis zu sieben
    tesseract-Nachkommen (gemessen 2026-10-26). Im gesunden Fall beenden die
    ihre Seite und gehen binnen ~4 s von selbst — aber der Fall, fuer den
    dieses Budget existiert, ist ein HAENGENDER tesseract, und dort ueberlebt
    er den Tod des Elternteils unbegrenzt, haelt einen Kern und das
    Staging-Verzeichnis. `start_new_session` gibt dem Kind eine eigene
    Prozessgruppe, damit die Sippe als Ganzes ansprechbar ist.
    """
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        out, err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        _killpg(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_REAP_GRACE)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode or 0, out, err)


def _killpg(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL an die Prozessgruppe des Kindes; sonst wenigstens an das Kind."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except OSError:
        # Die Gruppe ist schon weg, oder das Kind lief nie in einer eigenen.
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def output_name(title: str = "", when: datetime | None = None) -> str:
    """A human, sortable filename for the scan.

    The stage id (a uuid) is fine for a directory and terrible as a document
    name — it is what Renfield stores as `filename` and shows whenever it has
    nothing better. Renfield derives a SMART display name of its own from the
    document's content (generated_title, e.g. "Rechnung der … vom …"), so this
    only has to be honest and sortable, not clever: deriving a name from the OCR
    here would duplicate extraction the backend already owns and does better.
    """
    stamp = (when or datetime.now()).strftime("%Y-%m-%d-%H%M")
    base = _slug(title) or "Scan"
    return f"{base}-{stamp}.pdf"


def _slug(text: str, max_len: int = 60) -> str:
    """ASCII, filesystem- and header-safe. Umlauts are transliterated rather
    than stripped, so "Grundsteuerbescheid" survives a Content-Disposition."""
    if not text:
        return ""
    text = (text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
                .replace("ß", "ss"))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return text[:max_len].strip("-")


def assemble(pages: list[Path], stage: Path, config: Config, title: str = "") -> Path:
    """Correct each page in place, then build a searchable PDF."""
    for page in pages:
        correct_file(page, dpi=float(config.resolution), white_clip=config.white_clip)

    raw = stage / "scan-raw.pdf"
    out = stage / output_name(title)
    budget = assembly_budget(len(pages))
    try:
        done = _run_bounded(["img2pdf", "--output", str(raw), *[str(p) for p in pages]], budget)
    except subprocess.TimeoutExpired as exc:
        # Ohne das PDF gibt es nichts abzuliefern — das ist toedlich, anders
        # als ein Fehlschlag der Texterkennung weiter unten.
        raise AssemblyError(f"img2pdf timed out after {budget:.0f}s") from exc
    except OSError as exc:
        raise AssemblyError(f"img2pdf failed: {exc}") from exc
    # 🛑 `_run_bounded` prueft den Rueckgabewert NICHT — es ersetzt
    # `subprocess.run(..., check=True)`, also muss der Riegel hier stehen.
    if done.returncode != 0:
        raise AssemblyError(
            f"img2pdf failed (exit {done.returncode}): "
            f"{done.stderr.decode('utf-8', 'replace').strip()[:400]}"
        )

    reason = ""
    try:
        ocr = _run_bounded(
            ["ocrmypdf", "-l", config.ocr_languages, "--skip-text", "--optimize", "0",
             "--quiet", str(raw), str(out)],
            budget,
        )
    except subprocess.TimeoutExpired:
        reason = f"timed out after {budget:.0f}s"
    except OSError as exc:
        reason = f"could not be started: {exc}"
    else:
        if ocr.returncode != 0:
            reason = ocr.stderr.decode("utf-8", "replace").strip()[:400]

    if reason:
        # Do NOT discard the reason: a bad language, missing tessdata, an
        # unreadable page or an exhausted budget all land here, and "OCR
        # failed" alone is undebuggable. Ein Zeitablauf ist nur eine weitere
        # Art, auf die die Texterkennung nicht zustande kam — das Dokument
        # geht deswegen NICHT verloren.
        logger.warning("OCR failed, keeping un-OCR'd PDF: %s", reason)
        raw.replace(out)
    else:
        raw.unlink(missing_ok=True)
    return out
