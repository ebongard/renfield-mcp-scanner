"""Configuration: env scalars plus the mounted target registry.

DIVERGENCE FROM THE SIBLING INGEST MCPs, deliberate: they push to exactly one
backend and read a single ``RENFIELD_URL``. This server routes ONE scanner into
N independent instances, so the base URL and the push token move onto each
target. There is no global backend URL here, and there must not be one — a
single default is exactly the "silently files into the wrong instance" failure
the design forbids.

Secrets are never in the YAML: a target names the ENV VAR holding its token
(``token_env``) and it is resolved at use time, raising with the missing name.
That is what lets the registry be a ConfigMap (or a plain file) while the tokens
live in a Secret, or in the macOS Keychain via the LaunchAgent.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("renfield-mcp-scanner.config")

DEFAULT_MODE = "Color"
DEFAULT_SOURCE = "ADF Duplex"

#: What a caller may ask for at request time (`scan_document(sides=...)`), and
#: the SANE source each word maps to. Empty = the configured `source`.
SIDES = {"duplex": "ADF Duplex", "simplex": "ADF Front"}

#: `blank_backs` modes (see `Config.blank_backs`).
BLANK_BACKS_OFF, BLANK_BACKS_MEASURE, BLANK_BACKS_DROP = "off", "measure", "drop"
BLANK_BACKS_MODES = (BLANK_BACKS_OFF, BLANK_BACKS_MEASURE, BLANK_BACKS_DROP)


class MissingTokenError(RuntimeError):
    """A target's push token env var is unset. Names the variable."""


class ScanTarget(BaseModel):
    """One destination instance. Adding one is config, never code."""

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    # The ONLY thing the classifier is ever told about this target (Phase 3).
    # Keep it factual: it is the whole basis of a cross-boundary decision.
    description: str = ""
    base_url: str = Field(min_length=1)
    token_env: str = Field(min_length=1)
    # Opaque. THAT backend resolves it to owner/tier/kb; an unknown id is
    # rejected server-side, so a stolen token cannot pick a sphere.
    scan_profile_id: str = Field(min_length=1)
    # Phase 2: the barcode value on this target's printed separator sheet.
    separator_payload: str = ""
    # PEM bundle to verify this target's TLS with. A self-hosted backend often
    # serves a private/self-signed cert: curl may accept it from the OS trust
    # store while Python does NOT (httpx verifies against certifi), so a push
    # fails with CERTIFICATE_VERIFY_FAILED even though the URL works in a shell.
    # Empty = the default trust store. Never disable verification instead.
    ca_bundle: str = ""
    # The names people SAY for this target ("Haushalt", a person's name, a
    # spelling with a hyphen). They live here, in the operator's private file,
    # so no repo has to name a target for an agent to reach it.
    aliases: list[str] = Field(default_factory=list)

    def token(self) -> str:
        value = os.environ.get(self.token_env, "").strip()
        if not value:
            raise MissingTokenError(
                f"target {self.id!r}: env var {self.token_env} is unset or empty"
            )
        return value


class TargetsFile(BaseModel):
    targets: list[ScanTarget]

    @model_validator(mode="after")
    def _unique_ids(self) -> "TargetsFile":
        if not self.targets:
            raise ValueError("targets file declares no targets")
        seen = [t.id for t in self.targets]
        dupes = {i for i in seen if seen.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate target ids: {sorted(dupes)}")
        payloads = [t.separator_payload for t in self.targets if t.separator_payload]
        pdupes = {p for p in payloads if payloads.count(p) > 1}
        if pdupes:
            # Two targets sharing a separator payload would make a printed sheet
            # ambiguous, and the sheet is a DETERMINISTIC routing layer — an
            # ambiguous one silently becomes a guess.
            raise ValueError(f"duplicate separator payloads: {sorted(pdupes)}")
        return self


class Config(BaseModel):
    targets: list[ScanTarget]
    staging_dir: Path
    device: str = ""             # empty = autodetect the first Fujitsu
    resolution: int = 300
    mode: str = DEFAULT_MODE
    source: str = DEFAULT_SOURCE
    page_height_mm: float = 297.0     # A4. SANE defaults to US Letter — see pipeline.
    # A4 width. SANE's fujitsu default is US Letter width (215.9 mm), so every
    # page came out 2550 px wide with a 2.95 mm strip beside the paper on each
    # side. sane-fujitsu(5): page-width "is used by scanner to determine
    # centering of scan coordinates when using the ADF" — the window is centred
    # on the paper path, so 210 mm does not clip a centred A4 sheet. Set
    # SCANNER_PAGE_WIDTH_MM=215.9 to return to the previous geometry.
    page_width_mm: float = 210.0
    white_clip: float = 0.90
    ocr_languages: str = "deu+eng"
    # Blank BACK sides of a duplex scan (blank.py). Dark by default.
    #   off     — nothing is measured; the scan is exactly as before.
    #   measure — every back side is measured and the verdict LOGGED (numbers
    #             only); nothing is dropped and SANE's --swskip is unchanged.
    #             This is the calibration mode: run it before `drop`.
    #   drop    — a back side goes only when every measurement agrees; any
    #             doubt keeps it. SANE's own --swskip is 0 then, because a page
    #             skipped inside the backend shifts the front/back parity.
    blank_backs: Literal["off", "measure", "drop"] = BLANK_BACKS_OFF
    # Mean luminance at a 1/4-scale render, after colour correction (the audit's
    # measure). Blank backs measured 254-255; text pages reach 255 too, which is
    # why this alone never decides.
    blank_min_luminance: float = 253.0
    # Ink pixel: luminance below this, after correction. Probe 2026-10-31 (S1500
    # cast, 3x3 median): the darkest duplex show-through (raw 0.86 of paper)
    # lands at 242-246; a light pencil stroke or grey print at raw 0.80 lands at
    # 225. 232 sits between with margin to both (raw ~0.82 of paper).
    blank_ink_level: int = 232
    # ... or a channel spread of at least this. Coloured show-through measured
    # up to 13 (18 before the median); pale stamps that only colour reveals
    # (luminance above the level) measured 30. 24 leaves margin to both.
    blank_ink_chroma: int = 24
    # Ink area at full resolution, inside the margin, at or above which the page
    # is KEPT. 2 mm² is a few millimetres of pen stroke; a signature measures
    # tens of mm². Sensor noise is median-filtered away before counting.
    blank_ink_floor_mm2: float = 2.0
    # Border excluded from the ink count: paper edges and their shadow lines.
    blank_margin_mm: float = 5.0
    push_timeout_seconds: float = 120.0
    # 🛑 Hier stand `max_concurrent_pushes` und wurde NIE gelesen (entfernt
    # 2026-10-26). Wer die Pushes doch begrenzen will, braucht ein echtes
    # Schloss: `retry_pending_scans` ist ein eigenes Werkzeug OHNE das
    # Scan-Schloss (`server.py:194`), und der Scan laeuft im Hintergrund —
    # zwei Pushes koennen also ueberlappen. Die Annahme „seriell durch
    # Konstruktion" ist falsch, sie war nur folgenlos, weil das Feld
    # wirkungslos war. Im Review zu #14 belegt.
    # Der blaue Knopf am Gerät startet einen Scan. 🛑 AUS als Vorgabe, und das
    # ist eine MESSUNG, keine Meinung: auf dem ScanSnap S1500 schliessen sich
    # „Knopf jederzeit" und „verklemmt nie" gegenseitig aus.
    #
    #   * Der Knopfimpuls dauert 200–250 ms und HAELT NICHT (gemessen, s.
    #     Modulkopf von `button.py`). Wer ihn fangen will, muss mindestens
    #     fuenfmal je Sekunde fragen.
    #   * Jede Frage ist ein `scanimage -d … -A`, also ein voller
    #     Geraete-Oeffnungsvorgang. Nach einigen hundert davon ist das Geraet
    #     fuer SANE unansprechbar (`open … failed: Invalid argument`), und nur
    #     Aus-/Einschalten hilft — belegt null Selbstheilungen.
    #   * Die IORegistry traegt KEINE Knopf-Eigenschaft (2026-10-26
    #     nachgesehen), es gibt also kein freies Signal, das das Oeffnen
    #     ersetzen koennte.
    #   * Im verklemmten Zustand steht `CurrentPowerState == MaxPowerState`
    #     (2026-10-26 gemessen): das Geraet SCHLAEFT nicht. Es ist unser
    #     Abfragen, nicht Stromsparen.
    #
    # Der Wächter ist damit die einzige periodische Aufgabe, die das Geraet
    # anfasst — aus heisst: im Ruhezustand NULL Oeffnungen. Scannen geht ueber
    # einen angeforderten Scan (Chat, Weboberfläche, `scan_document`).
    #
    # `SCANNER_BUTTON_WATCH=true` schaltet ihn wieder an — fuer Geraete, deren
    # Sensor den Druck haelt, oder um die Dosis sauber auszumessen. Dann
    # verklemmt dieses Modell wieder.
    button_watch: bool = False
    # DELIBERATELY stricter than a suggestion would need. simba_classify can sit
    # at a lower bar because it only prefills a picker and a human corrects it;
    # above THIS threshold a document is filed across a trust boundary with
    # nobody looking. Anything below lands in the review queue, which is a mild
    # inconvenience — a misroute is not undoable.
    route_auto_threshold: float = 0.95
    # OpenAI-compatible endpoint for the L3 classifier. Empty disables L3
    # entirely: an undecided scan then simply waits for a human.
    classifier_url: str = ""
    classifier_model: str = ""
    staging_retention_days: int = 30
    # Ab welchem Alter ein wartender Stapel beim Start als WARNUNG gemeldet
    # wird, statt nur als Zahl in der Startzeile. Ein Tag: ein Scan, den
    # binnen eines Tages niemand abgeholt hat, ist ein Befund — ob er auf eine
    # Entscheidung wartet oder aus einem Hardwarefehler stammt. 0 schaltet die
    # Meldung ab. Vorgeschichte: ein Stapel mit `reason: scanner_fault` lag
    # sieben Tage da und erschien bei jedem Start nur als „1 pending".
    staging_warn_after_days: float = 1.0
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 9093
    # Authenticates A CALLER TO THIS SERVER (the opposite direction from a
    # target's push token). One entry per caller — an instance can then be
    # revoked without disturbing the others. Required whenever mcp_host is not
    # loopback. Populated from SCANNER_MCP_TOKEN_<CALLER> env vars.
    mcp_tokens: dict[str, str] = Field(default_factory=dict)
    # Which TARGET each caller is — the return path for scan-job completion
    # events (base_url + push token of that instance). Populated from
    # SCANNER_CALLER_TARGET_<CALLER>=<target id>. A caller without a mapping
    # still gets its scan done; only the completion event cannot be sent.
    caller_targets: dict[str, str] = Field(default_factory=dict)
    # How long one completion event keeps being retried (capped exponential
    # backoff). Matches Renfield's 24 h requester record: an outage shorter than
    # that must not lose the outcome — a count-based budget gave up after ~28 min.
    job_event_retry_hours: float = 24.0
    # Completion events sent at once to ONE target. After an outage every finished
    # job has an open event; unbounded they would all hit the instance the moment
    # it recovers. Only the HTTP request holds a slot, not the backoff sleep.
    job_event_concurrency: int = Field(default=2, ge=1)
    # NO default target, deliberately. A scan that cannot be routed must reach a
    # human; it must never fall back to "whichever instance is first".

    def target_by_id(self, target_id: str) -> ScanTarget | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def resolve_target(self, spoken: str) -> tuple[ScanTarget | None, str]:
        """Map what a person said to ONE target, or explain why not.

        An id wins first (exactly, then ignoring case), so every caller that
        passes an id is unaffected. Then id, label and aliases are compared
        ignoring case, spaces, hyphens and apostrophes -- and nothing looser.
        🛑 No suffix is stripped: a fallback from "Andreas" to "Andrea" files
        one person's paper into another person's instance with full
        confidence. A form people actually say ("Omas Renfield") is listed as
        an alias instead. Two targets answering to one name is refused.
        """
        exact = self.target_by_id(spoken) or next(
            (t for t in self.targets if t.id.casefold() == spoken.casefold()), None)
        if exact is not None:
            return exact, ""
        key = _loose(spoken)
        hits = [t for t in self.targets
                if key and key in {_loose(n) for n in (t.id, t.label, *t.aliases)}]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, (f"target {spoken!r} is ambiguous "
                          f"({', '.join(t.id for t in hits)}); known: {self.known_targets()}")
        return None, f"unknown target {spoken!r}; known: {self.known_targets()}"

    def known_targets(self) -> list[str]:
        """Ids and labels -- what `list_scanners` already shows every caller.

        Aliases stay out: they are the names of people, and the error goes to
        whichever instance called, not only to the one the alias belongs to."""
        return [f"{t.id} ({t.label})" for t in self.targets]

    @property
    def single_target(self) -> ScanTarget | None:
        """n == 1 short-circuits the whole routing layer."""
        return self.targets[0] if len(self.targets) == 1 else None


def _loose(name: str) -> str:
    return re.sub(r"[\s\-_'\u2019`]+", "", name.casefold())


def load_targets(path: str | Path) -> list[ScanTarget]:
    """Pure: parse + validate the registry. No env, no I/O beyond the read."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return TargetsFile.model_validate(raw).targets


def _caller_tokens() -> dict[str, str]:
    """Collect SCANNER_MCP_TOKEN_<CALLER> vars, plus the legacy singular one.

    The legacy SCANNER_MCP_TOKEN keeps working as the caller "default" so an
    existing install does not lock itself out on upgrade.
    """
    out: dict[str, str] = {}
    legacy = os.environ.get("SCANNER_MCP_TOKEN", "").strip()
    if legacy:
        out["default"] = legacy
    for key, value in os.environ.items():
        if key.startswith("SCANNER_MCP_TOKEN_") and value.strip():
            out[key[len("SCANNER_MCP_TOKEN_"):].lower()] = value.strip()
    return out


def _caller_targets(target_ids: set[str]) -> dict[str, str]:
    """Collect SCANNER_CALLER_TARGET_<CALLER>=<target id>.

    A mapping to an unknown target is dropped with a warning rather than kept:
    it could never be delivered, and failing the whole server over the return
    path would also stop the scans themselves."""
    out: dict[str, str] = {}
    prefix = "SCANNER_CALLER_TARGET_"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or not value.strip():
            continue
        caller, target_id = key[len(prefix):].lower(), value.strip()
        if target_id not in target_ids:
            logger.warning("%s names unknown target %r — no completion events for "
                           "caller %r", key, target_id, caller)
            continue
        out[caller] = target_id
    return out


def load_config() -> Config:
    """Konfiguration aus der Umgebung.

    🛑 **Kein Vorgabe-Literal in dieser Funktion.** Die Vorgabe steht genau
    einmal, am Feld des Modells; `_env` holt sie von dort. Vorher doppelte
    diese Funktion sie bei 17 von 18 Feldern (`env("SCANNER_RESOLUTION",
    "300")` gegen `resolution: int = 300`). Zwei Quellen für denselben Wert
    driften — und die des Modells war die wirkungslose, weil der Dienst seine
    Konfiguration ausschliesslich hierueber baut. Am 2026-10-26 wurde das
    teuer: das Umstellen von `button_watch` auf `False` am Modell haette
    NICHTS getan, solange hier `"true"` stand.

    Wer ein Feld hinzufuegt, schreibt die Vorgabe ans Modell und ruft hier nur
    `_env("feld", "SCANNER_NAME", cast)`. Ein Strukturtest haelt zu, dass
    niemand doch wieder ein Literal hinschreibt.
    """
    env = os.environ.get

    def _env(field: str, name: str, cast=str):
        """Der Wert aus der Umgebung, sonst die Vorgabe DES MODELLS."""
        raw = env(name, "").strip()
        return cast(raw) if raw else _default(field)

    targets_yaml = env("SCANNER_TARGETS_YAML", "").strip()
    if not targets_yaml:
        raise ValueError("SCANNER_TARGETS_YAML is required (path to the target registry)")
    targets = load_targets(targets_yaml)
    logger.info("loaded %d routing target(s): %s", len(targets), [t.id for t in targets])
    caller_targets = _caller_targets({t.id for t in targets})
    return Config(
        targets=targets,
        # `staging_dir` ist am Modell PFLICHT (kein Vorgabewert), hat also nur
        # HIER eine — die einzige Stelle, an der ein Literal richtig ist.
        staging_dir=Path(env("SCANNER_STAGING_DIR") or "~/.renfield-scanner/staging").expanduser(),
        device=_env("device", "SCANNER_DEVICE"),
        resolution=_env("resolution", "SCANNER_RESOLUTION", int),
        mode=_env("mode", "SCANNER_MODE"),
        source=_env("source", "SCANNER_SOURCE"),
        page_height_mm=_env("page_height_mm", "SCANNER_PAGE_HEIGHT_MM", float),
        page_width_mm=_env("page_width_mm", "SCANNER_PAGE_WIDTH_MM", float),
        white_clip=_env("white_clip", "SCANNER_WHITE_CLIP", float),
        ocr_languages=_env("ocr_languages", "SCANNER_OCR_LANGUAGES"),
        blank_backs=_choice("SCANNER_BLANK_BACKS", _default("blank_backs"),
                            BLANK_BACKS_MODES, env),
        blank_min_luminance=_env("blank_min_luminance", "SCANNER_BLANK_MIN_LUMINANCE", float),
        blank_ink_level=_env("blank_ink_level", "SCANNER_BLANK_INK_LEVEL", int),
        blank_ink_chroma=_env("blank_ink_chroma", "SCANNER_BLANK_INK_CHROMA", int),
        blank_ink_floor_mm2=_env("blank_ink_floor_mm2", "SCANNER_BLANK_INK_FLOOR_MM2", float),
        blank_margin_mm=_env("blank_margin_mm", "SCANNER_BLANK_MARGIN_MM", float),
        push_timeout_seconds=_env("push_timeout_seconds", "SCANNER_PUSH_TIMEOUT_SECONDS", float),
        route_auto_threshold=_env("route_auto_threshold", "SCANNER_ROUTE_AUTO_THRESHOLD", float),
        classifier_url=_env("classifier_url", "SCANNER_CLASSIFIER_URL"),
        classifier_model=_env("classifier_model", "SCANNER_CLASSIFIER_MODEL"),
        staging_retention_days=_env("staging_retention_days", "SCANNER_STAGING_RETENTION_DAYS", int),
        staging_warn_after_days=_env("staging_warn_after_days", "SCANNER_PENDING_WARN_DAYS", float),
        mcp_host=_env("mcp_host", "SCANNER_MCP_HOST"),
        mcp_port=_env("mcp_port", "SCANNER_MCP_PORT", int),
        mcp_tokens=_caller_tokens(),
        caller_targets=caller_targets,
        job_event_retry_hours=_env("job_event_retry_hours", "SCANNER_JOB_EVENT_RETRY_HOURS", float),
        job_event_concurrency=_env("job_event_concurrency", "SCANNER_JOB_EVENT_CONCURRENCY", int),
        button_watch=_flag("SCANNER_BUTTON_WATCH", _default("button_watch"), env),
    )


def _default(field: str):
    """Die Vorgabe, wie sie im Modell steht — die EINZIGE Quelle."""
    return Config.model_fields[field].default


def _choice(name: str, default: str, allowed: tuple[str, ...], env) -> str:
    """One of a fixed set of words from the environment, with the model's default.

    Same rule as `_flag`: an unknown value falls back to the default LOUDLY —
    a typo must neither switch a mode on nor pass unnoticed.
    """
    raw = env(name, "").strip().lower()
    if not raw:
        return default
    if raw in allowed:
        return raw
    logger.warning("%s=%r is not one of %s — the default %r applies",
                   name, raw, "/".join(allowed), default)
    return default


_JA = ("1", "true", "yes", "on", "ja", "an")
_NEIN = ("0", "false", "no", "off", "nein", "aus")


def _flag(name: str, default: bool, env) -> bool:
    """Ein Ja/Nein-Schalter aus der Umgebung, mit der Vorgabe des Modells.

    🛑 Eine AUSDRÜCKLICHE Ja-Menge, keine „alles ausser nein". Die erste
    Fassung las `raw not in ("0","false","no","off")`, also ergaben „nein",
    „aus", „maybe" und der Tippfehler „flase" ein JA. Solange die Vorgabe AN
    war, war das harmlos — ein Tippfehler traf dieselbe Entscheidung. Seit der
    Knopfwächter AUS als Vorgabe ist, dreht ein Tippfehler ihn AN und setzt
    damit den Scanner fest. Nachsichtigkeit zeigte also in die gefaehrliche
    Richtung.

    Unbekanntes faellt auf die Vorgabe zurueck, aber LAUT: stillschweigend die
    sichere Seite zu waehlen verschweigt dem Betreiber, dass seine Einstellung
    nicht gilt.
    """
    raw = env(name, "").strip().lower()
    if not raw:
        return default
    if raw in _JA:
        return True
    if raw in _NEIN:
        return False
    logger.warning("%s=%r ist weder ja noch nein — es gilt die Vorgabe %r. "
                   "Erlaubt: %s bzw. %s", name, raw, default,
                   "/".join(_JA), "/".join(_NEIN))
    return default
