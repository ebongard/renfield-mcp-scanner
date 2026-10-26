"""Das Zeitbudget des Zusammenbaus.

Ohne Grenze war `assemble` die einzige Stelle, an der ein einziger haengender
Kindprozess den blauen Knopf dauerhaft toetet: beide Aufrufe laufen ueber
`asyncio.to_thread` und sind nicht abbrechbar, `JobManager._active` bleibt
gesetzt, jedes `start()` antwortet `busy` — und `watcher_health` meldet weiter
`ok`. Die Tests halten beides fest: dass die Grenze greift, und dass ein
Ablauf der Texterkennung das Dokument NICHT kostet.
"""
import logging
import subprocess
import time

import pytest

from renfield_mcp_scanner import pdf as pdfmod
from renfield_mcp_scanner.config import Config, ScanTarget
from renfield_mcp_scanner.pdf import (
    ASSEMBLY_OVERHEAD,
    ASSEMBLY_PER_PAGE,
    AssemblyError,
    assemble,
    assembly_budget,
)


def _cfg(tmp_path):
    return Config(
        targets=[ScanTarget(id="household", label="H", base_url="http://b:8000",
                            token_env="TOK_H", scan_profile_id="d")],
        staging_dir=tmp_path / "st",
    )


# ---------------------------------------------------------------- das Budget

def test_budget_scales_with_the_page_count():
    assert assembly_budget(10) - assembly_budget(9) == pytest.approx(ASSEMBLY_PER_PAGE)


def test_budget_covers_the_measured_cost_with_room():
    # Gemessen 2026-10-26: img2pdf ~1,6 s/Seite, ocrmypdf ~1,15 s/Seite. Faellt
    # das Budget je Seite jemals unter das Dreifache davon, verliert ein
    # belasteter Rechner die Texterkennung echter Scans.
    assert ASSEMBLY_PER_PAGE >= 3 * 1.6


def test_an_empty_page_list_still_gets_a_budget():
    # Ein Budget von 0 waere eine Grenze, die sofort ablaeuft.
    assert assembly_budget(0) >= ASSEMBLY_OVERHEAD


# ------------------------------------------------- die Sippe stirbt mit

def test_the_timeout_takes_the_whole_family_down():
    """🛑 Der Kern: `subprocess.run(timeout=)` toetet nur das Kind.

    ocrmypdf haelt bis zu sieben tesseract-Nachkommen. Haengt einer von denen,
    ueberlebt er den Tod des Elternteils unbegrenzt. Dieser Test baut genau
    diese Form nach — ein Kind, das einen langlebigen Enkel hinterlaesst — und
    faellt, sobald jemand auf `subprocess.run(timeout=...)` zurueckbaut.
    """
    # Das Kind startet einen Enkel, der 300 s lebt, und haengt selbst.
    script = "sleep 300 & sleep 300"

    with pytest.raises(subprocess.TimeoutExpired):
        pdfmod._run_bounded(["/bin/sh", "-c", script], 1.0)

    # Der Enkel darf nicht mehr laufen. Gegengemessen: mit abgeschaltetem
    # `killpg` bleiben hier zwei Prozesse stehen, der Test wird rot.
    time.sleep(0.5)
    survivors = subprocess.run(
        ["pgrep", "-f", "sleep 300"], capture_output=True, text=True, check=False
    ).stdout.split()
    assert survivors == [], f"Enkel ueberlebten den Zeitablauf: {survivors}"


def test_a_healthy_run_returns_its_output():
    done = pdfmod._run_bounded(["/bin/sh", "-c", "printf hallo; exit 3"], 10.0)
    assert done.returncode == 3
    assert done.stdout == b"hallo"


# --------------------------------------------- img2pdf: Ablauf ist toedlich

def test_an_img2pdf_timeout_fails_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfmod, "correct_file", lambda *a, **kw: None)
    monkeypatch.setattr(pdfmod, "_run_bounded",
                        lambda argv, budget: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(argv, budget)))
    with pytest.raises(AssemblyError, match="img2pdf timed out"):
        assemble([tmp_path / "p0001.png"], tmp_path, _cfg(tmp_path))


def test_a_nonzero_img2pdf_exit_fails_the_job(tmp_path, monkeypatch):
    """`_run_bounded` ersetzt `subprocess.run(check=True)` und prueft den
    Rueckgabewert NICHT — der Riegel muss im Aufrufer stehen."""
    monkeypatch.setattr(pdfmod, "correct_file", lambda *a, **kw: None)
    monkeypatch.setattr(
        pdfmod, "_run_bounded",
        lambda argv, budget: subprocess.CompletedProcess(argv, 1, b"", b"kaputtes PNG"))
    with pytest.raises(AssemblyError, match="kaputtes PNG"):
        assemble([tmp_path / "p0001.png"], tmp_path, _cfg(tmp_path))


# ------------------------------- ocrmypdf: Ablauf kostet nur die Texterkennung

def test_an_ocr_timeout_keeps_the_document(tmp_path, monkeypatch, caplog):
    """Ein Zeitablauf der Texterkennung ist nur eine weitere Art, auf die sie
    nicht zustande kam. Das Dokument geht NICHT verloren."""
    monkeypatch.setattr(pdfmod, "correct_file", lambda *a, **kw: None)

    def fake(argv, budget):
        if argv[0] == "img2pdf":
            (tmp_path / "scan-raw.pdf").write_bytes(b"%PDF-1.4 roh")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise subprocess.TimeoutExpired(argv, budget)

    monkeypatch.setattr(pdfmod, "_run_bounded", fake)
    with caplog.at_level(logging.WARNING):
        out = assemble([tmp_path / "p0001.png"], tmp_path, _cfg(tmp_path))

    assert out.exists() and out.read_bytes() == b"%PDF-1.4 roh"
    assert not (tmp_path / "scan-raw.pdf").exists()   # umbenannt, nicht kopiert
    assert "timed out" in caplog.text                 # der Grund bleibt lesbar


def test_a_failed_ocr_still_reports_its_reason(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pdfmod, "correct_file", lambda *a, **kw: None)

    def fake(argv, budget):
        if argv[0] == "img2pdf":
            (tmp_path / "scan-raw.pdf").write_bytes(b"%PDF-1.4 roh")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 2, b"", b"tessdata fuer 'xyz' fehlt")

    monkeypatch.setattr(pdfmod, "_run_bounded", fake)
    with caplog.at_level(logging.WARNING):
        out = assemble([tmp_path / "p0001.png"], tmp_path, _cfg(tmp_path))
    assert out.read_bytes() == b"%PDF-1.4 roh"
    assert "tessdata" in caplog.text
