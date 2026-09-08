"""Staging is the reason an unrouted scan never touches an instance."""
import os
from datetime import datetime, timedelta
from renfield_mcp_scanner.staging import Staging


def test_root_is_private(tmp_path):
    s = Staging(tmp_path / "st")
    assert oct(s.root.stat().st_mode)[-3:] == "700"


def test_keep_records_the_reason(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    s.keep(stage, reason="unrouted", detail="two targets configured")
    pending = s.pending()
    assert len(pending) == 1
    assert pending[0]["reason"] == "unrouted"
    assert pending[0]["stage_id"] == stage.name


def test_discard_removes(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    s.discard(stage)
    assert not stage.exists() and s.pending() == []


def test_pending_counts_pages(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    for i in range(3):
        (stage / "pages" / f"p{i:04d}.png").write_bytes(b"x")
    s.keep(stage, reason="scanner_fault")
    assert s.pending()[0]["pages"] == 3


def test_unreadable_metadata_still_lists(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    (stage / "stage.json").write_text("{not json")
    assert s.pending()[0]["reason"] == "unreadable_metadata"


def test_purge_respects_retention(tmp_path):
    s = Staging(tmp_path / "st")
    old, new = s.new_stage(), s.new_stage()
    old_ts = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(old, (old_ts, old_ts))
    assert s.purge_expired(30) == 1
    assert new.exists() and not old.exists()


def test_purge_disabled_by_zero(tmp_path):
    s = Staging(tmp_path / "st")
    s.new_stage()
    assert s.purge_expired(0) == 0 and len(s.pending()) == 1


def test_stage_ids_never_collide(tmp_path):
    # A timestamp alone collides for scans started in the same instant, and a
    # collision means two documents sharing a directory.
    s = Staging(tmp_path / "st")
    ids = {s.new_stage().name for _ in range(500)}
    assert len(ids) == 500
