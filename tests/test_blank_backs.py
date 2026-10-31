"""Blank BACK sides of a duplex scan are dropped — on evidence, never on a guess.

The synthetic pages are full A4 at 300 dpi with the S1500's measured raw blue
cast (paper peak R226 G236 B253), so every page runs through the real colour
correction first, exactly as in production. Thresholds are in millimetres, so
the drawn marks are sized in millimetres too.
"""
from __future__ import annotations

import json
import logging
import math
import random
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from renfield_mcp_scanner import blank
from renfield_mcp_scanner.config import Config, ScanTarget
from renfield_mcp_scanner.contract import StageAction
from renfield_mcp_scanner.pusher import PushOutcome
from renfield_mcp_scanner.sane import ScanResult
from renfield_mcp_scanner.staging import Staging
from renfield_mcp_scanner.tools import blank_rules, resolve_source, scan_document

DPI = 300
W, H = 2480, 3508                       # A4 at 300 dpi
PAPER = (226, 236, 253)                 # raw S1500 paper peak, blue cast
PX_PER_MM = DPI / 25.4


def mm(v: float) -> int:
    return round(v * PX_PER_MM)


def _rules(**over) -> blank.BlankRules:
    cfg = Config(targets=[ScanTarget(id="h", label="h", base_url="http://b",
                                     token_env="T", scan_profile_id="d")],
                 staging_dir="/tmp/unused", **over)
    return blank_rules(cfg)


RULES = _rules()


# --- synthetic pages ---------------------------------------------------------

def page_blank() -> Image.Image:
    return Image.new("RGB", (W, H), PAPER)


def page_noisy_blank() -> Image.Image:
    """Blank paper as a real sensor delivers it: grain on the paper, a few
    thousand isolated dark pixels, a 1 px paper-edge shadow line, dust specks
    well below a millimetre. 3000 isolated pixels alone are ~21 mm² — far
    above the floor if they were counted unfiltered."""
    rnd = random.Random(7)
    im = page_blank()
    px = im.load()
    for _ in range(60000):                       # paper grain, +-4 levels
        x, y = rnd.randrange(W), rnd.randrange(H)
        d = rnd.randint(-4, 4)
        px[x, y] = tuple(max(0, min(255, c + d)) for c in PAPER)
    for _ in range(3000):                        # isolated dark sensor pixels
        px[rnd.randrange(W), rnd.randrange(H)] = (60, 60, 60)
    d = ImageDraw.Draw(im)
    d.line([(mm(2), 0), (mm(2), H)], fill=(120, 120, 130), width=1)   # edge shadow
    for _ in range(4):                           # dust, ~0.25 mm
        x, y = rnd.randrange(mm(20), W - mm(20)), rnd.randrange(mm(20), H - mm(20))
        d.ellipse([x, y, x + 2, y + 2], fill=(50, 50, 50))
    return im


def page_with_signature() -> Image.Image:
    """A lone small signature, 35 x 12 mm, 0.4 mm blue ballpoint, low right."""
    im = page_blank()
    d = ImageDraw.Draw(im)
    x0, y0 = W - mm(60), H - mm(50)
    pts = [(x0 + mm(35) * t / 200, y0 + mm(6) * math.sin(t / 9.0) * (1 - t / 260))
           for t in range(200)]
    d.line(pts, fill=(40, 50, 140), width=max(1, mm(0.4)))
    return im


def page_with_initials() -> Image.Image:
    """The smallest mark that must survive: initials, ~8 x 5 mm, 0.4 mm pen."""
    im = page_blank()
    d = ImageDraw.Draw(im)
    x0, y0 = mm(150), mm(200)
    d.line([(x0, y0 + mm(5)), (x0 + mm(2), y0), (x0 + mm(4), y0 + mm(5)),
            (x0 + mm(6), y0), (x0 + mm(8), y0 + mm(5))],
           fill=(30, 30, 90), width=mm(0.4))
    return im


def page_with_stamp() -> Image.Image:
    """A round red office stamp, 30 mm across, 0.8 mm ring, nothing else."""
    im = page_blank()
    d = ImageDraw.Draw(im)
    cx, cy, r = mm(105), mm(150), mm(15)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(190, 45, 55), width=mm(0.8))
    d.ellipse([cx - r + mm(3), cy - r + mm(3), cx + r - mm(3), cy + r - mm(3)],
              outline=(190, 45, 55), width=mm(0.5))
    return im


def page_with_highlighter() -> Image.Image:
    """A yellow highlighter mark: LIGHT (luminance well above the dark level)
    but strongly coloured — only the chroma half of the ink measure sees it."""
    im = page_blank()
    ImageDraw.Draw(im).rectangle([mm(40), mm(100), mm(80), mm(105)], fill=(230, 222, 95))
    return im


def page_dark_paper_with_text() -> Image.Image:
    """Light-grey recycled paper with printed text. The colour correction
    rescales the grey paper to WHITE, so after it the page mean looks almost
    as bright as a blank page — the text must keep it."""
    im = Image.new("RGB", (W, H), (200, 202, 196))
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=56)
    for i in range(6):
        d.text((mm(25), mm(40) + i * mm(10)), "Rechnung Nummer 4711 vom 31. Oktober",
               fill=(35, 35, 35), font=font)
    return im


def page_content() -> Image.Image:
    im = page_blank()
    d = ImageDraw.Draw(im)
    for i in range(40):
        d.rectangle([mm(25), mm(30) + i * mm(6), mm(185), mm(30) + i * mm(6) + mm(2)],
                    fill=(20, 20, 20))
    return im


def _save(tmp_path, images) -> list:
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, im in enumerate(images, start=1):
        p = pages_dir / f"p{i:04d}.png"
        im.save(p, dpi=(DPI, DPI))
        out.append(p)
    return out


def no_text(image, languages, **_):
    """OCR stand-in for a page without letters. A signature or stamp yields no
    text either, so this is honest for every test that uses it."""
    return 0


def _judge(tmp_path, im, ocr=no_text):
    path = _save(tmp_path, [page_content(), im])[1]
    return blank.judge_back(path, 2, path.with_name("p0001.png"), RULES, ocr=ocr)


# --- one back side -----------------------------------------------------------

def test_a_blank_back_is_dropped(tmp_path):
    v = _judge(tmp_path, page_blank())
    assert v.drop and v.reason == "blank"
    assert v.mean_luminance >= RULES.min_luminance and v.ink_mm2 < RULES.ink_floor_mm2


def test_a_blank_back_with_scanner_noise_is_dropped(tmp_path):
    v = _judge(tmp_path, page_noisy_blank())
    assert v.drop, v


def test_a_small_signature_keeps_the_page(tmp_path):
    v = _judge(tmp_path, page_with_signature())
    assert not v.drop and v.reason == "ink", v
    # The signature barely moves the page mean: the luminance gate alone would
    # have dropped it. That is exactly why the ink floor exists.
    assert v.mean_luminance >= RULES.min_luminance


def test_initials_keep_the_page(tmp_path):
    v = _judge(tmp_path, page_with_initials())
    assert not v.drop and v.reason == "ink", v


def test_a_stamp_keeps_the_page(tmp_path):
    v = _judge(tmp_path, page_with_stamp())
    assert not v.drop and v.reason == "ink", v
    assert v.mean_luminance >= RULES.min_luminance


def test_a_light_coloured_mark_keeps_the_page(tmp_path):
    v = _judge(tmp_path, page_with_highlighter())
    assert not v.drop and v.reason == "ink", v


def test_a_dark_paper_page_with_text_is_kept(tmp_path):
    v = _judge(tmp_path, page_dark_paper_with_text())
    assert not v.drop, v


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_the_real_ocr_reads_the_dark_paper_page(tmp_path):
    """The text gate with the real engine, not a stand-in: on its own it keeps
    the page even if both image gates were fooled."""
    from renfield_mcp_scanner.pipeline import correct
    chars = blank.ocr_chars(correct(page_dark_paper_with_text()), "deu+eng", workdir=tmp_path)
    assert chars is not None and chars > RULES.max_chars
    assert blank.ocr_chars(correct(page_blank()), "deu+eng", workdir=tmp_path) <= RULES.max_chars
    # The probe image lives inside the (private) stage only while OCR runs.
    assert list(tmp_path.iterdir()) == []


def test_text_the_image_gates_miss_keeps_the_page(tmp_path):
    v = _judge(tmp_path, page_blank(), ocr=lambda im, lang: 200)
    assert not v.drop and v.reason == "text"


def test_ocr_that_cannot_run_keeps_the_page(tmp_path):
    v = _judge(tmp_path, page_blank(), ocr=lambda im, lang: None)
    assert not v.drop and v.reason == "ocr_unavailable"


def test_an_unreadable_page_is_kept(tmp_path):
    p = tmp_path / "p0002.png"
    p.write_bytes(b"not a png")
    v = blank.judge_back(p, 2, tmp_path / "p0001.png", RULES, ocr=no_text)
    assert not v.drop and v.reason == "unreadable"


# --- the whole scan ----------------------------------------------------------

def _select(pages, source="ADF Duplex"):
    kept, verdicts = blank.select(pages, source=source, rules=RULES, ocr=no_text)
    return [p.name for p in kept], [v.name for v in verdicts if v.drop]


def test_a_blank_front_is_never_dropped(tmp_path):
    pages = _save(tmp_path, [page_blank(), page_content(), page_blank(), page_blank()])
    kept, dropped = _select(pages)
    assert dropped == ["p0004.png"]
    assert "p0001.png" in kept and "p0003.png" in kept


def test_a_single_page_scan_is_never_touched(tmp_path):
    pages = _save(tmp_path, [page_blank()])
    assert _select(pages) == (["p0001.png"], [])


def test_an_all_blank_scan_never_becomes_empty(tmp_path):
    pages = _save(tmp_path, [page_blank()] * 4)
    kept, dropped = _select(pages)
    assert kept == ["p0001.png", "p0003.png"] and dropped == ["p0002.png", "p0004.png"]


def test_an_odd_duplex_page_count_drops_nothing(tmp_path):
    """Parity unproven (a page went missing somewhere) — decide nothing on it."""
    pages = _save(tmp_path, [page_content(), page_blank(), page_content()])
    assert _select(pages) == (["p0001.png", "p0002.png", "p0003.png"], [])


def test_a_simplex_scan_drops_nothing(tmp_path):
    pages = _save(tmp_path, [page_content(), page_blank()])
    assert _select(pages, source="ADF Front") == (["p0001.png", "p0002.png"], [])


def test_set_aside_keeps_the_evidence_and_logs_numbers_only(tmp_path, caplog):
    pages = _save(tmp_path, [page_content(), page_blank()])
    _, verdicts = blank.select(pages, source="ADF Duplex", rules=RULES, ocr=no_text)
    with caplog.at_level(logging.INFO, logger="renfield-mcp-scanner.blank"):
        dropped = blank.set_aside(tmp_path, tmp_path / "pages", verdicts,
                                  job_id="job123", total=2)
    assert [v.name for v in dropped] == ["p0002.png"]
    assert not (tmp_path / "pages" / "p0002.png").exists()
    assert (tmp_path / "dropped" / "p0002.png").exists()
    record = json.loads((tmp_path / "dropped" / blank.EVIDENCE_FILE).read_text())
    assert record[0]["page"] == 2 and record[0]["front"] == "p0001.png"
    line = next(r.getMessage() for r in caplog.records if "dropped blank back" in r.getMessage())
    assert "job123" in line and "mean_luminance=" in line and "ink_mm2=" in line


# --- through scan_document ---------------------------------------------------

def _cfg(tmp_path, *ids, **over):
    return Config(
        targets=[ScanTarget(id=i, label=i, base_url="http://b:8000",
                            token_env=f"TOK_{i.upper()}", scan_profile_id="d")
                 for i in ids],
        staging_dir=tmp_path / "st", **over)


class _Scan:
    """Fake sane: writes the given images as pages and records its options."""

    def __init__(self, images):
        self.images, self.kw = images, None

    async def __call__(self, device, out_dir, **kw):
        self.kw = kw
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for i, im in enumerate(self.images, start=1):
            p = out_dir / f"p{i:04d}.png"
            im.save(p, dpi=(DPI, DPI))
            pages.append(p)
        return ScanResult(pages=pages, log="")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_HOUSEHOLD", "t")
    monkeypatch.setenv("TOK_OFFICE", "t")

    async def dev(_):
        return "dev"

    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.find_device", dev)
    monkeypatch.setattr(blank, "ocr_chars", no_text)
    state = {"pushed": [], "assembled": [], "action": StageAction.DISCARD}

    class _P:
        def __init__(self, *a, **k):
            pass

        async def push(self, filename, data, meta):
            state["pushed"].append(meta)
            return PushOutcome(state["action"], status="ingested", document_id=1)

    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _P)

    def assemble(pages, stage, config, title=""):
        state["assembled"].append([p.name for p in pages])
        out = stage / "doc.pdf"
        out.write_bytes(b"%PDF")
        return out

    state["assemble"] = assemble
    return state


async def test_scan_drops_the_blank_back_before_assembly(tmp_path, monkeypatch, wired):
    scan = _Scan([page_content(), page_blank(), page_content(), page_with_signature()])
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", scan)
    out = await scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                              wired["assemble"], job_id="j1")
    assert out["ok"] and out["blank_backs_dropped"] == [2]
    assert wired["assembled"] == [["p0001.png", "p0003.png", "p0004.png"]]
    assert wired["pushed"][0]["pages"] == 3
    # Parity needs SANE's own skip OFF; A4 width goes to the scanner.
    assert scan.kw["skip_blank"] == 0.0
    assert scan.kw["source"] == "ADF Duplex" and scan.kw["page_width_mm"] == 210.0


async def test_evidence_stays_until_the_document_is_delivered(tmp_path, monkeypatch, wired):
    wired["action"] = StageAction.KEEP              # push did not go through
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch",
                        _Scan([page_content(), page_blank()]))
    out = await scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                              wired["assemble"])
    stage = tmp_path / "st" / out["stage_id"]
    assert (stage / "dropped" / "p0002.png").exists()
    assert (stage / "dropped" / blank.EVIDENCE_FILE).exists()


async def test_simplex_is_a_request_option(tmp_path, monkeypatch, wired):
    scan = _Scan([page_content(), page_blank()])
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", scan)
    out = await scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                              wired["assemble"], sides="simplex")
    assert scan.kw["source"] == "ADF Front"
    assert "skip_blank" not in scan.kw, "simplex keeps SANE's skip exactly as before"
    assert "blank_backs_dropped" not in out and out["pages"] == 2


async def test_an_unknown_sides_value_is_refused_before_scanning(tmp_path, monkeypatch, wired):
    scan = _Scan([page_content()])
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", scan)
    out = await scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                              wired["assemble"], sides="both")
    assert out["error_code"] == "invalid_sides" and scan.kw is None


def test_resolve_source_defaults_to_the_configured_source(tmp_path):
    cfg = _cfg(tmp_path, "household")
    assert resolve_source(cfg, "") == "ADF Duplex"
    assert resolve_source(cfg, " Simplex ") == "ADF Front"
    assert resolve_source(cfg, "ADF Back") is None


async def test_switched_off_the_scan_is_unchanged(tmp_path, monkeypatch, wired):
    scan = _Scan([page_content(), page_blank()])
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", scan)
    out = await scan_document(_cfg(tmp_path, "household", drop_blank_backs=False),
                              Staging(tmp_path / "st"), wired["assemble"])
    assert "skip_blank" not in scan.kw
    assert wired["assembled"] == [["p0001.png", "p0002.png"]]
    assert "blank_backs_dropped" not in out


async def test_a_separator_back_is_dropped_and_evidence_follows_its_document(
        tmp_path, monkeypatch, wired):
    """Sheet(household) + its blank back, then a document whose back is blank.
    The separator's back must not open the document as a blank first page, and
    the document's own dropped back travels into the stage that delivers it."""
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch",
                        _Scan([page_content(), page_blank(), page_content(), page_blank()]))
    monkeypatch.setattr("renfield_mcp_scanner.separator.decode_page",
                        lambda p: "household" if p.name == "p0001.png" else None)
    wired["action"] = StageAction.KEEP              # keep the sub-stage to inspect it
    out = await scan_document(_cfg(tmp_path, "household", "office"),
                              Staging(tmp_path / "st"), wired["assemble"])
    assert out["blank_backs_dropped"] == [2, 4]
    assert wired["assembled"] == [["p0003.png"]]
    sub = tmp_path / "st" / out["documents"][0]["stage_id"]
    assert (sub / "dropped" / "p0004.png").exists()
    record = json.loads((sub / "dropped" / blank.EVIDENCE_FILE).read_text())
    assert [r["name"] for r in record] == ["p0004.png"]


# --- plumbing: config, job, SANE command -------------------------------------

def test_settings_come_from_the_environment(monkeypatch, tmp_path):
    from renfield_mcp_scanner.config import load_config
    reg = tmp_path / "targets.yaml"
    reg.write_text("targets:\n  - id: household\n    label: H\n    base_url: http://b\n"
                   "    token_env: TOK_H\n    scan_profile_id: d\n")
    monkeypatch.setenv("SCANNER_TARGETS_YAML", str(reg))
    for k in list(__import__("os").environ):
        if k.startswith(("SCANNER_BLANK_", "SCANNER_DROP_", "SCANNER_PAGE_WIDTH")):
            monkeypatch.delenv(k)
    cfg = load_config()
    assert cfg.drop_blank_backs is True and cfg.page_width_mm == 210.0
    assert (cfg.blank_max_chars, cfg.blank_min_luminance, cfg.blank_ink_floor_mm2) == (30, 253.0, 2.0)

    monkeypatch.setenv("SCANNER_DROP_BLANK_BACKS", "false")
    monkeypatch.setenv("SCANNER_PAGE_WIDTH_MM", "215.9")
    monkeypatch.setenv("SCANNER_BLANK_INK_FLOOR_MM2", "5")
    monkeypatch.setenv("SCANNER_BLANK_MAX_CHARS", "10")
    cfg = load_config()
    assert cfg.drop_blank_backs is False and cfg.page_width_mm == 215.9
    assert cfg.blank_ink_floor_mm2 == 5.0 and cfg.blank_max_chars == 10


async def test_the_job_carries_sides_and_its_id_into_the_scan(tmp_path):
    from renfield_mcp_scanner import jobs as j
    seen = {}

    async def run_scan(target, title, **kw):
        seen.update(kw)
        return {"ok": True, "routed": True}

    class _N:
        async def deliver(self, job, store):
            pass

    manager = j.JobManager(j.JobStore(tmp_path), run_scan, _N())
    started = await manager.start(caller="household", target="", title="", sides="simplex")
    await manager.wait_idle()
    assert seen == {"sides": "simplex", "job_id": started["job_id"]}


async def test_scan_batch_passes_the_page_width_and_the_skip(tmp_path, monkeypatch):
    from renfield_mcp_scanner import sane
    argv = []

    async def fake_run(*args, **kw):
        argv.extend(args)
        return 0, "out of documents"

    monkeypatch.setattr(sane, "_run", fake_run)
    await sane.scan_batch("fujitsu:x", tmp_path, source="ADF Front", mode="Color",
                          resolution=300, page_height_mm=297.0, page_width_mm=210.0,
                          skip_blank=0.0)
    assert argv[argv.index("--page-width") + 1] == "210.0"
    assert argv[argv.index("--swskip") + 1] == "0.0"
    assert argv[argv.index("--source") + 1] == "ADF Front"

    argv.clear()
    await sane.scan_batch("fujitsu:x", tmp_path, source="ADF Duplex", mode="Color",
                          resolution=300, page_height_mm=297.0)
    assert "--page-width" not in argv, "no width given = the backend default, as before"
    assert argv[argv.index("--swskip") + 1] == "2.0"


async def test_a_crashing_blank_check_keeps_every_page(tmp_path, monkeypatch, wired):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(blank, "select", boom)
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch",
                        _Scan([page_content(), page_blank()]))
    out = await scan_document(_cfg(tmp_path, "household"), Staging(tmp_path / "st"),
                              wired["assemble"])
    assert out["ok"] and wired["assembled"] == [["p0001.png", "p0002.png"]]
