"""Retrying scans kept after a failed push.

Staging keeps a scan on ANY ambiguous outcome, which is what stops a failed push
costing a re-feed of the paper. Keeping without a retry path just means "kept
forever", so this is the recovery leg of that contract — and it must refuse to
retry a scan that has no destination, because pushing one somewhere would be
exactly the guess the design forbids.
"""
import pathlib
import pytest

from renfield_mcp_scanner.config import Config, ScanTarget
from renfield_mcp_scanner.contract import IngestStatus
from renfield_mcp_scanner.staging import Staging
from renfield_mcp_scanner.tools import retry_pending_scans


def _cfg(tmp_path, *ids):
    return Config(
        targets=[ScanTarget(id=i, label=i, base_url="http://b:8000",
                            token_env=f"TOK_{i.upper()}", scan_profile_id="d")
                 for i in ids],
        staging_dir=tmp_path / "st",
    )


def _staged(staging, *, target, with_pdf=True):
    stage = staging.new_stage()
    (stage / "pages" / "p0001.png").write_bytes(b"x")
    if with_pdf:
        (stage / f"{stage.name}.pdf").write_bytes(b"%PDF-1.4")
    staging.keep(stage, reason="transport_error", detail="backend down", target=target)
    return stage


class _Pusher:
    def __init__(self, *a, **k): pass
    async def push(self, filename, data, meta):
        from renfield_mcp_scanner.pusher import PushOutcome
        from renfield_mcp_scanner.contract import StageAction
        return PushOutcome(StageAction.DISCARD, status=IngestStatus.INGESTED.value,
                           document_id=99)


async def test_retry_pushes_and_clears_the_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_FILES", "t")
    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Pusher)
    st = Staging(tmp_path / "st")
    stage = _staged(st, target="files")
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st)
    assert out["retried"] == 1
    assert out["results"][0]["resolved"] is True
    assert out["results"][0]["renfield_document_id"] == 99
    assert not stage.exists()


async def test_unrouted_scan_is_never_retried(tmp_path, monkeypatch):
    # No destination recorded => a retry would have to GUESS one. Refuse.
    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Pusher)
    st = Staging(tmp_path / "st")
    stage = _staged(st, target=None)
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st)
    assert out["results"][0]["skipped"] == "unrouted"
    assert stage.exists(), "an unrouted scan must be preserved, not consumed"


async def test_target_no_longer_configured_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Pusher)
    st = Staging(tmp_path / "st")
    stage = _staged(st, target="removed")
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st)
    assert out["results"][0]["skipped"] == "unknown_target"
    assert stage.exists()


async def test_missing_token_is_skipped_not_lost(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK_FILES", raising=False)
    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Pusher)
    st = Staging(tmp_path / "st")
    stage = _staged(st, target="files")
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st)
    assert out["results"][0]["skipped"] == "missing_token"
    assert stage.exists()


async def test_stage_without_a_pdf_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_FILES", "t")
    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Pusher)
    st = Staging(tmp_path / "st")
    stage = _staged(st, target="files", with_pdf=False)
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st)
    assert out["results"][0]["skipped"] == "no_pdf"
    assert stage.exists()


async def test_stage_id_filter_retries_only_that_one(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_FILES", "t")
    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Pusher)
    st = Staging(tmp_path / "st")
    a, b = _staged(st, target="files"), _staged(st, target="files")
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st, stage_id=a.name)
    assert out["retried"] == 1
    assert not a.exists() and b.exists()


async def test_a_failed_retry_keeps_the_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_FILES", "t")

    class _Fail(_Pusher):
        async def push(self, *a, **k):
            from renfield_mcp_scanner.pusher import PushOutcome
            from renfield_mcp_scanner.contract import StageAction
            return PushOutcome(StageAction.KEEP, detail="still down")

    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _Fail)
    st = Staging(tmp_path / "st")
    stage = _staged(st, target="files")
    out = await retry_pending_scans(_cfg(tmp_path, "files"), st)
    assert out["results"][0]["resolved"] is False
    assert stage.exists()
    # and the target survives the re-keep, so it stays retryable
    assert st.pending()[0]["target"] == "files"


async def test_scan_result_names_the_id_system(tmp_path, monkeypatch):
    """An unqualified `document_id` got reported to the user as a Paperless id
    when it was the Renfield one — and the document was not in Paperless at all
    yet. The result must say which system each id belongs to."""
    import renfield_mcp_scanner.tools as T
    src = pathlib.Path(T.__file__).read_text()
    assert '"renfield_document_id"' in src
    assert '"document_id":' not in src, "bare document_id invites mislabelling"
    assert "paperless" in src.lower()
