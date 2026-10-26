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
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("renfield-mcp-scanner.config")

DEFAULT_MODE = "Color"
DEFAULT_SOURCE = "ADF Duplex"


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
    white_clip: float = 0.90
    ocr_languages: str = "deu+eng"
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

    @property
    def single_target(self) -> ScanTarget | None:
        """n == 1 short-circuits the whole routing layer."""
        return self.targets[0] if len(self.targets) == 1 else None


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
        white_clip=_env("white_clip", "SCANNER_WHITE_CLIP", float),
        ocr_languages=_env("ocr_languages", "SCANNER_OCR_LANGUAGES"),
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
