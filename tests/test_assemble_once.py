"""Der Zusammenbau läuft EINMAL je Dokument, nicht zweimal.

🛑 Warum das zählt: `assemble` ist der teuerste Schritt der Kette. Gemessen
am 2026-10-26 kostet `img2pdf` ~1,6 s/Seite und `ocrmypdf` ~1,15 s/Seite; ein
voller Einzug (duplex 100 Seiten) lief damit rund 69 Minuten doppelt. Und weil
`output_name` minutengenau stempelt, hinterliessen zwei Läufe über eine
Minutengrenze ZWEI PDFs im Stapel — eine davon eine Waise.

Zwei Pfade bauten doppelt:
  * Der Klassifizierer baut eine Sonden-PDF, um Text daraus zu lesen; `_deliver`
    baute dieselbe PDF danach noch einmal.
  * `route_scan` baute sie bedingt und liess `_deliver` sie danach
    bedingungslos erneut bauen — der bedingte Aufruf war wirkungslos.

Diese Tests ZÄHLEN die Aufrufe. Eine Zusicherung über Laufzeit wäre flatterig,
eine über die Anzahl ist es nicht.
"""
import pathlib

import pytest

from renfield_mcp_scanner.classifier import Classification
from renfield_mcp_scanner.config import Config, ScanTarget
from renfield_mcp_scanner.contract import StageAction
from renfield_mcp_scanner.pusher import PushOutcome
from renfield_mcp_scanner.staging import Staging
from renfield_mcp_scanner.tools import route_scan, scan_document


def _cfg(tmp_path, *ids, **kw):
    return Config(
        targets=[ScanTarget(id=i, label=i, base_url="http://b:8000",
                            token_env=f"TOK_{i.upper()}", scan_profile_id="d")
                 for i in ids],
        staging_dir=tmp_path / "st",
        **kw,
    )


class _Zaehler:
    """Ein `assemble`, das mitschreibt, wie oft und womit es gerufen wurde."""
    def __init__(self):
        self.aufrufe: list[tuple[int, str]] = []

    def __call__(self, pages, stage, config, title=""):
        self.aufrufe.append((len(pages), stage.name))
        out = stage / "doc.pdf"
        out.write_bytes(b"%PDF")
        return out


class _Scan:
    def __init__(self, n): self.n = n

    async def __call__(self, device, out_dir, **kw):
        from renfield_mcp_scanner.sane import ScanResult
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for i in range(1, self.n + 1):
            p = out_dir / f"p{i:04d}.png"; p.write_bytes(b"x"); pages.append(p)
        return ScanResult(pages=pages, log="")


async def _aio(v):
    return v


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("TOK_HOUSEHOLD", "t")
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.find_device",
                        lambda d: _aio("dev"))
    monkeypatch.setattr("renfield_mcp_scanner.separator.decode_page", lambda p: None)
    gedrueckt = []

    class _P:
        def __init__(self, base, token, timeout=120.0, ca_bundle=""):
            pass
        async def push(self, filename, data, meta):
            gedrueckt.append(filename)
            return PushOutcome(StageAction.DISCARD, status="ingested",
                               document_id=len(gedrueckt))

    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _P)
    return gedrueckt


async def test_the_classifier_path_assembles_once(tmp_path, monkeypatch, wired):
    """Der Klassifizierer-Pfad: Sonden-PDF UND Lieferung — aber EIN Bau."""
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(3))
    monkeypatch.setattr("renfield_mcp_scanner.tools.classifier.extract_text",
                        lambda pdf: "irgendein Text")

    async def _classify(text, targets, *, url, model):
        return Classification(target_id="household", confidence=0.99, evidence="Anschrift")

    monkeypatch.setattr("renfield_mcp_scanner.tools.classifier.classify", _classify)

    zaehler = _Zaehler()
    cfg = _cfg(tmp_path, "household", "office",
               classifier_url="http://llm", classifier_model="m")
    out = await scan_document(cfg, Staging(tmp_path / "st"), zaehler)

    assert out["ok"] is True, out
    assert len(zaehler.aufrufe) == 1, (
        f"assemble lief {len(zaehler.aufrufe)}x — die Sonden-PDF wird nicht "
        f"wiederverwendet: {zaehler.aufrufe}")


async def test_a_declared_target_assembles_once(tmp_path, monkeypatch, wired):
    """Ohne Klassifizierer gab es nie ein Doppel — das muss so bleiben."""
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(2))
    zaehler = _Zaehler()
    out = await scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                              zaehler, target="household")
    assert out["ok"] is True, out
    assert len(zaehler.aufrufe) == 1, zaehler.aufrufe


async def test_route_scan_reuses_an_existing_pdf(tmp_path, monkeypatch, wired):
    """🛑 Trägt der Stapel schon eine PDF, wird GAR NICHT gebaut.

    Das ist der Normalfall nach einem Klassifizierer-Lauf, der `unrouted`
    endete: die Sonden-PDF liegt dort.
    """
    st = Staging(tmp_path / "st")
    stage = st.new_stage()
    (stage / "pages").mkdir(parents=True, exist_ok=True)
    for i in (1, 2):
        (stage / "pages" / f"p{i:04d}.png").write_bytes(b"x")
    (stage / "doc.pdf").write_bytes(b"%PDF")      # schon vorhanden
    st.keep(stage, reason="unrouted")

    zaehler = _Zaehler()
    out = await route_scan(_cfg(tmp_path, "household"), st, zaehler,
                           stage.name, "household")
    assert out["ok"] is True, out
    assert zaehler.aufrufe == [], (
        f"assemble lief trotz vorhandener PDF: {zaehler.aufrufe}")


async def test_route_scan_assembles_once_without_a_pdf(tmp_path, monkeypatch, wired):
    """Ohne PDF genau EIN Bau — vorher waren es zwei (ein bedingter, dann ein
    bedingungsloser in `_deliver`)."""
    st = Staging(tmp_path / "st")
    stage = st.new_stage()
    (stage / "pages").mkdir(parents=True, exist_ok=True)
    for i in (1, 2):
        (stage / "pages" / f"p{i:04d}.png").write_bytes(b"x")
    st.keep(stage, reason="unrouted")

    zaehler = _Zaehler()
    out = await route_scan(_cfg(tmp_path, "household"), st, zaehler,
                           stage.name, "household")
    assert out["ok"] is True, out
    assert len(zaehler.aufrufe) == 1, (
        f"assemble lief {len(zaehler.aufrufe)}x statt 1x: {zaehler.aufrufe}")


# ---------------------------------------------------------------------------
# 🛑 Eine leere Datei wird NIE eingereicht
# ---------------------------------------------------------------------------

async def test_an_empty_pdf_is_never_filed_and_the_pages_survive(tmp_path, monkeypatch, wired):
    """Der Datenverlust, den die Wiederverwendung eingebaut hatte.

    `assemble` legte die Zwischendatei als `scan-raw.pdf` an und räumte sie auf
    keinem Fehlerpfad weg; ein gescheitertes `img2pdf` liess sie mit 0 BYTE
    liegen (gemessen 2026-10-26). Damit sah sie für jeden `glob("*.pdf")` wie
    eine fertige Lieferung aus, `route_scan` griff sie als `sorted(...)[0]`
    (sie sortiert vor jedem kleingeschriebenen Titel), und seit der
    Wiederverwendung baute `_deliver` nicht mehr neu. Die leere Datei wurde
    eingereicht, die Gegenseite antwortete `ingested`, und `discard` räumte die
    SEITEN weg — das Papier müsste erneut durch den Einzug.

    Zwei Riegel, hier beide geprüft: die Zwischendatei heisst `.tmp` (kein
    `*.pdf`-Glob sieht sie), und `_deliver` weigert sich grundsätzlich, eine
    leere Datei einzureichen.
    """
    st = Staging(tmp_path / "st")
    stage = st.new_stage()
    (stage / "pages").mkdir(parents=True, exist_ok=True)
    for i in (1, 2):
        (stage / "pages" / f"p{i:04d}.png").write_bytes(b"x")
    # Genau der Rückstand eines gescheiterten `img2pdf` — mit dem ALTEN Namen,
    # damit der Test auch die Umbenennung absichert.
    (stage / "scan-raw.pdf").write_bytes(b"")
    st.keep(stage, reason="scanner_fault")

    zaehler = _Zaehler()
    out = await route_scan(_cfg(tmp_path, "household"), st, zaehler,
                           stage.name, "household")

    assert out["ok"] is False, "eine leere Datei darf nicht eingereicht werden"
    assert out["error_code"] == "empty_pdf", out
    # Und das Entscheidende: die Seiten sind NOCH DA.
    assert (stage / "pages" / "p0001.png").exists(), (
        "die Seiten wurden geräumt — das Papier müsste erneut durch den Einzug")
    assert list(wired) == [], f"es wurde trotzdem gepusht: {wired}"


def test_the_intermediate_file_is_invisible_to_a_pdf_glob(tmp_path):
    """Die Wurzel: `assemble` darf keine Datei anlegen, die ein `*.pdf`-Glob
    für eine Lieferung halten kann."""
    from renfield_mcp_scanner import pdf as pdfmod

    gesehen = {}

    def fake(argv, budget):
        if argv[0] == "img2pdf":
            # Der Name der Zwischendatei steht hinter `--output`.
            roh = pathlib.Path(argv[argv.index("--output") + 1])
            gesehen["roh"] = roh
            roh.write_bytes(b"%PDF-1.4")
            return __import__("subprocess").CompletedProcess(argv, 0, b"", b"")
        return __import__("subprocess").CompletedProcess(argv, 0, b"", b"")

    monkeypatch_ = pytest.MonkeyPatch()
    monkeypatch_.setattr(pdfmod, "correct_file", lambda *a, **kw: None)
    monkeypatch_.setattr(pdfmod, "_run_bounded", fake)
    try:
        pdfmod.assemble([tmp_path / "p0001.png"], tmp_path, _cfg(tmp_path, "household"))
    finally:
        monkeypatch_.undo()

    assert gesehen["roh"].suffix != ".pdf", (
        f"die Zwischendatei heisst {gesehen['roh'].name} — ein `glob('*.pdf')` "
        "koennte sie fuer eine fertige Lieferung halten")


async def test_segment_does_not_block_the_event_loop(tmp_path, monkeypatch, wired):
    """🛑 `separator.segment` öffnet JEDE Seite mit PIL und sucht mit `pyzbar`
    einen Strichcode. Gemessen an echten Seiten (2550×3508): 411 ms je Seite —
    ein voller Duplex-Einzug fror die Schleife 41 s ein.

    Derselbe Fehler wie bei `assemble`, und derselbe Nachweis: die Schleife
    muss ANDERE Arbeit erledigen können, während `segment` noch rechnet. Nicht
    über Zeitmessung, sondern deterministisch.
    """
    import asyncio
    import threading
    import time

    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(2))

    segment_laeuft = threading.Event()
    beantwortet = threading.Event()
    schleife_war_frei = []

    echtes_segment = __import__(
        "renfield_mcp_scanner.separator", fromlist=["segment"]).segment

    def langsames_segment(pages):
        segment_laeuft.set()
        time.sleep(0.3)
        schleife_war_frei.append(bool(beantwortet.is_set()))
        return echtes_segment(pages)

    monkeypatch.setattr("renfield_mcp_scanner.separator.segment", langsames_segment)

    async def andere_anfrage():
        while not segment_laeuft.is_set():
            await asyncio.sleep(0.001)
        beantwortet.set()

    await asyncio.gather(
        scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                      _Zaehler(), target="household"),
        andere_anfrage(),
    )
    assert schleife_war_frei == [True], (
        "die Ereignisschleife war waehrend `segment` blockiert — ein langer "
        "Stapel laesst das Backend den Scanner fuer tot erklaeren")
