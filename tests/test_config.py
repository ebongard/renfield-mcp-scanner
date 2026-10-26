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


# ---------------------------------------------------------------------------
# Eine Einstellung, EINE Quelle
# ---------------------------------------------------------------------------

def test_the_button_flag_has_exactly_one_default(monkeypatch, tmp_path):
    """🛑 `load_config` darf die Vorgabe NICHT selbst noch einmal hinschreiben.

    Bis 2026-10-26 stand hier `env("SCANNER_BUTTON_WATCH", "true")`, waehrend
    das Modell `True` trug — zwei Quellen fuer dieselbe Einstellung. Die
    Vorgabe des Modells war damit toter Code, denn der Dienst baut seine
    Konfiguration ausschliesslich ueber `load_config`. Beim Abschalten des
    Knopfwaechters haette genau das die Aenderung unwirksam gemacht: Modell
    `False`, Dienst weiter `True`.
    """
    from renfield_mcp_scanner.config import Config, load_config

    ziele = tmp_path / "targets.yaml"
    ziele.write_text(
        "targets:\n"
        "  - id: household\n"
        "    label: Haushalt\n"
        "    base_url: http://b:8000\n"
        "    token_env: TOK_H\n"
        "    scan_profile_id: d\n"
    )
    monkeypatch.setenv("SCANNER_TARGETS_YAML", str(ziele))
    monkeypatch.setenv("TOK_H", "t")
    monkeypatch.delenv("SCANNER_BUTTON_WATCH", raising=False)
    erwartet = Config.model_fields["button_watch"].default
    assert load_config().button_watch is erwartet, (
        "load_config traegt eine eigene Vorgabe — die des Modells ist dann wirkungslos")


def test_the_button_watch_default_is_off():
    """Der Knopfwaechter ist AUS als Vorgabe. Begruendung (gemessen) steht am
    Feld in `config.py`: der Impuls haelt nicht, jede Abfrage oeffnet das
    Geraet, und nach einigen hundert Oeffnungen ist der S1500 fuer SANE
    unansprechbar. Diese Zusicherung ist der Riegel dagegen, dass die Vorgabe
    unbemerkt zurueckkippt."""
    from renfield_mcp_scanner.config import Config

    assert Config.model_fields["button_watch"].default is False


def test_no_default_literal_survives_in_the_loader():
    """🛑 Der Riegel fuer die KLASSE, nicht fuer den einen Fall.

    Am 2026-10-26 doppelte `load_config` die Modellvorgabe bei 17 von 18
    Feldern (`env("SCANNER_RESOLUTION", "300")` gegen `resolution: int = 300`).
    Zwei Quellen fuer denselben Wert driften, und die des Modells ist die
    wirkungslose — der Dienst baut seine Konfiguration ausschliesslich ueber
    `load_config`. Der erste Anlauf reparierte nur `button_watch`; dieser Test
    haelt ALLE zu.
    """
    import re
    from pathlib import Path

    from renfield_mcp_scanner.config import Config

    quelle = Path(__file__).resolve().parents[1] / "renfield_mcp_scanner" / "config.py"
    text = quelle.read_text()
    rumpf = text[text.index("def load_config"):text.index("def _default(")]

    # `feld=...env("NAME", "LITERAL")...` — ein zweites Vorgabe-Literal.
    schuldig = [
        feld for feld, _name in re.findall(r'(\w+)=\s*[\w.]*\(?\s*env\("([A-Z_0-9]+)",\s*"', rumpf)
        if feld in Config.model_fields
        and repr(Config.model_fields[feld].default) != "PydanticUndefined"
    ]
    assert not schuldig, (
        "diese Felder tragen in load_config ein eigenes Vorgabe-Literal, "
        f"obwohl das Modell schon eins hat: {sorted(set(schuldig))}. "
        "Benutze `_env(\"feld\", \"SCANNER_NAME\", cast)`.")


def test_an_unknown_flag_value_falls_back_loudly(caplog):
    """Ein Tippfehler darf den Knopfwaechter nicht ANschalten — das setzt das
    Geraet fest. Und er darf nicht lautlos verpuffen."""
    import logging

    from renfield_mcp_scanner.config import _flag

    with caplog.at_level(logging.WARNING):
        assert _flag("SCANNER_BUTTON_WATCH", False, lambda n, d="": "flase") is False
    assert "weder ja noch nein" in caplog.text
    # Die ausdrueckliche Ja-Menge gilt weiter, in beiden Sprachen.
    for wert in ("true", "1", "ja", "AN", " On "):
        assert _flag("X", False, lambda n, d="", w=wert: w) is True
    for wert in ("false", "0", "nein", "AUS", " off "):
        assert _flag("X", True, lambda n, d="", w=wert: w) is False
