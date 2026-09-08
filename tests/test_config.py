"""The registry is the 1..n design. Its invariants are load-bearing."""
import pytest
from pydantic import ValidationError
from renfield_mcp_scanner.config import (
    Config, MissingTokenError, ScanTarget, TargetsFile, load_targets,
)


def _raw(tid, sep=None):
    d = {"id": tid, "label": tid, "base_url": "http://b:8000",
         "token_env": f"TOK_{tid}", "scan_profile_id": "d"}
    if sep is not None:
        d["separator_payload"] = sep
    return d


def test_example_registry_parses():
    targets = load_targets("config/targets.example.yaml")
    assert targets and all(t.id and t.base_url and t.token_env for t in targets)


def test_no_tokens_in_the_example_registry():
    # Secrets must never be in a file that could become a ConfigMap.
    text = open("config/targets.example.yaml").read().lower()
    assert "token:" not in text and "password" not in text


def test_missing_token_names_the_variable(monkeypatch):
    t = ScanTarget(**_raw("primary"))
    monkeypatch.delenv("TOK_primary", raising=False)
    with pytest.raises(MissingTokenError, match="TOK_primary"):
        t.token()


def test_token_resolves_from_env(monkeypatch):
    monkeypatch.setenv("TOK_primary", "abc")
    assert ScanTarget(**_raw("primary")).token() == "abc"


def test_blank_token_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("TOK_primary", "   ")
    with pytest.raises(MissingTokenError):
        ScanTarget(**_raw("primary")).token()


def test_duplicate_ids_rejected():
    with pytest.raises((ValidationError, ValueError)):
        TargetsFile.model_validate({"targets": [_raw("a"), _raw("a")]})


def test_duplicate_separator_payloads_rejected():
    # A shared payload makes a printed sheet ambiguous, and the sheet is a
    # DETERMINISTIC routing layer — ambiguity there is a silent guess.
    with pytest.raises((ValidationError, ValueError)):
        TargetsFile.model_validate({"targets": [_raw("a", "X"), _raw("b", "X")]})


def test_distinct_separator_payloads_accepted():
    assert len(TargetsFile.model_validate(
        {"targets": [_raw("a", "X"), _raw("b", "Y")]}).targets) == 2


def test_empty_registry_rejected():
    with pytest.raises((ValidationError, ValueError)):
        TargetsFile.model_validate({"targets": []})


def test_single_target_property():
    one = Config(targets=[ScanTarget(**_raw("a"))], staging_dir="/tmp/x")
    two = Config(targets=[ScanTarget(**_raw("a")), ScanTarget(**_raw("b"))],
                 staging_dir="/tmp/x")
    assert one.single_target.id == "a"
    assert two.single_target is None


def test_a4_is_the_default_page_height():
    # SANE's own default is US Letter (279.364mm), which silently truncates
    # 17.7mm off every A4 page, footer included.
    assert Config(targets=[ScanTarget(**_raw("a"))], staging_dir="/tmp").page_height_mm == 297.0


def test_no_default_target_exists():
    # A scan that cannot be routed must reach a human, never a fallback.
    assert "default_target" not in Config.model_fields
