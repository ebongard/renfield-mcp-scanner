"""scan_document with separator sheets in the stack.

Splitting is unconditional; routing BY sheet applies only when the operator did
not declare a destination. The cases that matter are the ones where a naive
implementation loses or misfiles pages.
"""
import pathlib

import pytest

from renfield_mcp_scanner.config import Config, ScanTarget
from renfield_mcp_scanner.contract import StageAction
from renfield_mcp_scanner.pusher import PushOutcome
from renfield_mcp_scanner.staging import Staging
from renfield_mcp_scanner.tools import scan_document


def _cfg(tmp_path, *ids):
    return Config(
        targets=[ScanTarget(id=i, label=i, base_url="http://b:8000",
                            token_env=f"TOK_{i.upper()}", scan_profile_id="d")
                 for i in ids],
        staging_dir=tmp_path / "st",
    )


class _Scan:
    """Fake sane: writes N pages into the stage and reports them."""
    def __init__(self, n): self.n = n

    async def __call__(self, device, out_dir, **kw):
        from renfield_mcp_scanner.sane import ScanResult
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for i in range(1, self.n + 1):
            p = out_dir / f"p{i:04d}.png"; p.write_bytes(b"x"); pages.append(p)
        return ScanResult(pages=pages, log="")


def _assemble(pages, stage, config, title=""):
    out = stage / "doc.pdf"; out.write_bytes(b"%PDF"); return out


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_HOUSEHOLD", "t")
    monkeypatch.setenv("TOK_OFFICE", "t")
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.find_device",
                        lambda d: _aio("dev"))
    pushed = []

    class _P:
        def __init__(self, base, token, timeout=120.0, ca_bundle=""):
            self.base = base
        async def push(self, filename, data, meta):
            pushed.append(meta)
            return PushOutcome(StageAction.DISCARD, status="ingested",
                               document_id=len(pushed))

    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _P)
    return pushed


async def _aio(v):
    return v


def _marks(monkeypatch, mapping):
    monkeypatch.setattr("renfield_mcp_scanner.separator.decode_page",
                        lambda p: mapping.get(p.name))


async def test_stack_without_sheets_stays_one_document(tmp_path, monkeypatch, wired):
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(3))
    _marks(monkeypatch, {})
    st = Staging(tmp_path / "st")
    out = await scan_document(_cfg(tmp_path, "household"), st, _assemble)
    assert out["ok"] and not out.get("split")
    assert out["pages"] == 3 and len(wired) == 1


async def test_mixed_stack_becomes_several_documents(tmp_path, monkeypatch, wired):
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(5))
    # p1 = sheet(household), p2 p3 content, p4 = sheet(office), p5 content
    _marks(monkeypatch, {"p0001.png": "household", "p0004.png": "office"})
    st = Staging(tmp_path / "st")
    out = await scan_document(_cfg(tmp_path, "household", "office"), st, _assemble)
    assert out["split"] and out["segments"] == 2
    assert [d["target"] for d in out["documents"]] == ["household", "office"]
    # The separator pages are consumed, not filed.
    assert [d["pages"] for d in out["documents"]] == [2, 1]


async def test_declared_target_overrides_the_sheets(tmp_path, monkeypatch, wired):
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(4))
    _marks(monkeypatch, {"p0001.png": "office"})
    st = Staging(tmp_path / "st")
    out = await scan_document(_cfg(tmp_path, "household", "office"), st,
                              _assemble, target="household")
    # Still SPLIT at the sheet, but routed where the operator said.
    assert out["split"]
    assert all(d["target"] == "household" for d in out["documents"])


async def test_pages_before_the_first_sheet_are_not_misfiled(tmp_path, monkeypatch, wired):
    # The dangerous case: attributing them to the following sheet would file one
    # document's pages under the NEXT document's destination.
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(3))
    _marks(monkeypatch, {"p0002.png": "office"})
    st = Staging(tmp_path / "st")
    out = await scan_document(_cfg(tmp_path, "household", "office"), st, _assemble)
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["reason"] == "unrouted"
    assert out["documents"][0]["target"] == "office"
    assert out["ok"] is False, "a skipped segment must not report overall success"


async def test_sheet_naming_an_unconfigured_target_is_skipped(tmp_path, monkeypatch, wired):
    monkeypatch.setattr("renfield_mcp_scanner.tools.sane.scan_batch", _Scan(3))
    _marks(monkeypatch, {"p0001.png": "ghost"})
    st = Staging(tmp_path / "st")
    out = await scan_document(_cfg(tmp_path, "household"), st, _assemble)
    assert out["skipped"] and "not configured" in out["skipped"][0]["detail"]
    assert wired == [], "nothing may be pushed for an unknown target"
