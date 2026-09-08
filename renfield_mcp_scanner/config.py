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
    max_concurrent_pushes: int = 4
    health_poll_seconds: int = 60
    route_auto_threshold: float = 0.85
    staging_retention_days: int = 30
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 9093
    # Authenticates RENFIELD TO THIS SERVER (the opposite direction from a
    # target's push token). Required whenever mcp_host is not loopback.
    mcp_token: str = ""
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


def load_config() -> Config:
    env = os.environ.get
    targets_yaml = env("SCANNER_TARGETS_YAML", "").strip()
    if not targets_yaml:
        raise ValueError("SCANNER_TARGETS_YAML is required (path to the target registry)")
    targets = load_targets(targets_yaml)
    logger.info("loaded %d routing target(s): %s", len(targets), [t.id for t in targets])
    return Config(
        targets=targets,
        staging_dir=Path(env("SCANNER_STAGING_DIR", "~/.renfield-scanner/staging")).expanduser(),
        device=env("SCANNER_DEVICE", ""),
        resolution=int(env("SCANNER_RESOLUTION", "300")),
        mode=env("SCANNER_MODE", DEFAULT_MODE),
        source=env("SCANNER_SOURCE", DEFAULT_SOURCE),
        page_height_mm=float(env("SCANNER_PAGE_HEIGHT_MM", "297")),
        white_clip=float(env("SCANNER_WHITE_CLIP", "0.90")),
        ocr_languages=env("SCANNER_OCR_LANGUAGES", "deu+eng"),
        push_timeout_seconds=float(env("SCANNER_PUSH_TIMEOUT_SECONDS", "120")),
        max_concurrent_pushes=int(env("SCANNER_MAX_CONCURRENT_PUSHES", "4")),
        health_poll_seconds=int(env("SCANNER_HEALTH_POLL_SECONDS", "60")),
        route_auto_threshold=float(env("SCANNER_ROUTE_AUTO_THRESHOLD", "0.85")),
        staging_retention_days=int(env("SCANNER_STAGING_RETENTION_DAYS", "30")),
        mcp_host=env("SCANNER_MCP_HOST", "127.0.0.1"),
        mcp_port=int(env("SCANNER_MCP_PORT", "9093")),
        mcp_token=env("SCANNER_MCP_TOKEN", ""),
    )
