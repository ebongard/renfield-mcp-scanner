"""L3 classification.

Mirrors Renfield's simba_classify pattern, with one difference that governs
every case here: simba_classify prefills a picker and a human corrects it, while
this one — above its threshold — files a document across a trust boundary with
nobody looking. So every ambiguity resolves to "undecided", never to a guess.
"""
import pytest

from renfield_mcp_scanner.classifier import (
    Classification, _match_target, _parse_json, _prompt, classify,
)
from renfield_mcp_scanner.config import ScanTarget


def _t(i, desc=""):
    return ScanTarget(id=i, label=i.title(), description=desc,
                      base_url="http://b", token_env="T", scan_profile_id="d")


# --- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"target":"a"}', {"target": "a"}),
    ('```json\n{"target":"a"}\n```', {"target": "a"}),
    ('```\n{"target":"a"}\n```', {"target": "a"}),
    ('Antwort: {"target":"a"} — fertig', {"target": "a"}),
])
def test_tolerant_json_parsing(raw, expected):
    assert _parse_json(raw) == expected


@pytest.mark.parametrize("raw", ["", "kein json", "[1,2]", '"a string"', "null", "{"])
def test_unparseable_output_is_a_miss(raw):
    assert _parse_json(raw) is None


# --- the allowlist is the load-bearing part ----------------------------------

def test_allowlist_matches_case_insensitively():
    assert _match_target("Household", ["household", "xidra"]) == "household"


@pytest.mark.parametrize("value", [
    "haushalt",      # plausible, not configured
    "household ",    # trailing space is fine
    "hous",          # prefix is NOT a near-miss to be resolved generously
    "", None, 42, {"id": "household"},
])
def test_only_configured_ids_are_accepted(value):
    got = _match_target(value, ["household", "xidra"])
    assert got in (None, "household")
    if value == "household ":
        assert got == "household"
    else:
        assert got is None


def test_prompt_is_built_from_the_targets_own_descriptions():
    # This module must carry no notion of what a target IS — adding one is config.
    system, user = _prompt([_t("household", "Privatpost"), _t("xidra", "Firmenpost")], "x")
    assert "household: Privatpost" in user and "xidra: Firmenpost" in user
    assert "EMPFAENGER" in system, "the addressee is the signal, not the sender"


# --- every failure resolves to undecided -------------------------------------

async def test_empty_text_is_undecided():
    assert await classify("", [_t("a")], url="http://x", model="m") is None


async def test_no_targets_is_undecided():
    assert await classify("text", [], url="http://x", model="m") is None


async def test_no_model_is_undecided():
    assert await classify("text", [_t("a")], url="http://x", model="") is None


async def _with_reply(monkeypatch, content, status=200):
    class _Resp:
        status_code = status
        def json(self): return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return await classify("Rechnung", [_t("household"), _t("xidra")],
                          url="http://x/v1", model="m")


async def test_a_confident_valid_answer_is_returned(monkeypatch):
    got = await _with_reply(monkeypatch,
        '{"target":"xidra","confidence":0.97,"evidence":"An X GmbH"}')
    assert got == Classification("xidra", 0.97, "An X GmbH")


async def test_an_invented_target_is_a_miss_not_a_near_match(monkeypatch):
    # A model returning a plausible-but-unconfigured id must route NOWHERE.
    assert await _with_reply(monkeypatch,
        '{"target":"haushalt","confidence":0.99}') is None


async def test_unparseable_reply_is_undecided(monkeypatch):
    assert await _with_reply(monkeypatch, "Ich denke, es ist xidra.") is None


async def test_http_error_is_undecided(monkeypatch):
    assert await _with_reply(monkeypatch, '{"target":"xidra"}', status=500) is None


async def test_missing_confidence_defaults_to_zero(monkeypatch):
    # No confidence means no confidence — it must not sail past the threshold.
    got = await _with_reply(monkeypatch, '{"target":"xidra"}')
    assert got is not None and got.confidence == 0.0


@pytest.mark.parametrize("raw,expected", [('"hoch"', 0.0), ("1.7", 1.0), ("-3", 0.0)])
async def test_confidence_is_coerced_and_clamped(monkeypatch, raw, expected):
    got = await _with_reply(monkeypatch, '{"target":"xidra","confidence":%s}' % raw)
    assert got is not None and got.confidence == expected


async def test_unreachable_model_is_undecided(monkeypatch):
    class _Boom:
        def __init__(self, **k): pass
        async def __aenter__(self): raise OSError("no route to host")
        async def __aexit__(self, *a): return False
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    assert await classify("t", [_t("a")], url="http://x", model="m") is None
