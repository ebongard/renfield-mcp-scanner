"""Routing must settle on exactly one target, or refuse to guess."""
import pytest
from renfield_mcp_scanner.config import Config, ScanTarget
from renfield_mcp_scanner.router import route


def _t(tid, sep=""):
    return ScanTarget(id=tid, label=tid.title(), base_url="http://b:8000",
                      token_env=f"TOK_{tid.upper()}", scan_profile_id="d",
                      separator_payload=sep)


def _cfg(*ids):
    return Config(targets=[_t(i) for i in ids], staging_dir="/tmp/x")


def test_single_target_short_circuits():
    r = route(_cfg("only"))
    assert r.settled and r.layer == "single" and r.target.id == "only"


def test_declared_target_wins():
    r = route(_cfg("a", "b"), declared="b")
    assert r.settled and r.layer == "declared" and r.target.id == "b"


def test_declared_beats_single_target_too():
    r = route(_cfg("only"), declared="only")
    assert r.layer == "declared"


def test_unknown_target_is_refused_not_coerced():
    r = route(_cfg("a", "b"), declared="typo")
    assert not r.settled and "unknown target" in r.reason
    assert "'a'" in r.reason or "a" in r.reason


def test_multiple_targets_without_declaration_is_undecided():
    # The load-bearing case: NO fallback target exists, so this must not settle.
    r = route(_cfg("a", "b"))
    assert not r.settled and r.layer == "undecided" and r.confidence == 0.0


def test_there_is_no_default_target_field():
    assert not hasattr(_cfg("a", "b"), "default_target")
