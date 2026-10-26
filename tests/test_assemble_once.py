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
