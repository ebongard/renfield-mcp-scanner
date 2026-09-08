"""Pushes a finished scan into ONE target instance's folder-ingest endpoint.

Mirrors the sibling ingest MCPs' push client, with one structural difference
that matters: those serve a single backend, so they hold one base URL and one
token. This one is constructed PER TARGET, because the whole point is N
instances. Nothing here can choose a target — the caller has already decided,
and that decision is the router's, made under the design's invariant.

The metadata carries only routing and provenance. Never owner, tier or KB: the
receiving backend resolves ``scan_profile_id`` to those itself, so a stolen
token cannot escalate a tier or file into another sphere.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .contract import (
    CONTRACT_HEADER,
    HEALTH_PATH,
    INGEST_PATH,
    SCANNER_INGEST_CONTRACT_VERSION,
    StageAction,
    action_for_status,
)

logger = logging.getLogger("renfield-mcp-scanner.pusher")


@dataclass
class PushOutcome:
    action: StageAction
    fatal: bool = False
    status: str = ""
    document_id: int | None = None
    detail: str = ""
    http_status: int | None = None


class TargetPusher:
    def __init__(self, base_url: str, token: str, timeout_seconds: float = 120.0,
                 ca_bundle: str = ""):
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # A PEM path, or True for the default trust store. NEVER False — an
        # unverified push would send documents to whatever answers the name.
        self._verify: str | bool = ca_bundle or True
        self._headers = {
            "Authorization": f"Bearer {token}",
            CONTRACT_HEADER: SCANNER_INGEST_CONTRACT_VERSION,
        }

    async def push(self, filename: str, pdf: bytes, metadata: dict) -> PushOutcome:
        """POST one scanned PDF. Every ambiguous outcome resolves to KEEP.

        A scan cannot be regenerated without the paper going back through the
        feeder, so the bias is always to keep the staged copy. Only an
        acknowledged terminal status discards it.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout, verify=self._verify) as client:
                resp = await client.post(
                    f"{self._base}{INGEST_PATH}",
                    headers=self._headers,
                    files={"file": (filename, pdf, "application/pdf")},
                    data={"metadata": json.dumps(metadata)},
                )
        except httpx.HTTPError as exc:
            logger.warning("push transport error: %s", exc)
            return PushOutcome(StageAction.KEEP, detail=f"transport_error: {exc}")

        if resp.status_code in (401, 403):
            # Operator action required. Retrying just hammers the backend with a
            # bad token, so this stops the target rather than looping.
            logger.error("push rejected %s — token invalid for this target", resp.status_code)
            return PushOutcome(StageAction.KEEP, fatal=True,
                               detail="auth_rejected", http_status=resp.status_code)
        if resp.status_code != 200:
            # 503 = feature disabled or worker down. Normal, retryable.
            logger.info("push not accepted (HTTP %s)", resp.status_code)
            return PushOutcome(StageAction.KEEP, detail=f"http_{resp.status_code}",
                               http_status=resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            logger.warning("push returned 200 with unparseable body")
            return PushOutcome(StageAction.KEEP, detail="bad_json", http_status=200)

        seen = str(body.get("contract_version", ""))
        if seen and seen != SCANNER_INGEST_CONTRACT_VERSION:
            # Log, then proceed leniently — never fail a real document on skew.
            logger.warning("ingest contract skew: backend=%s ours=%s",
                           seen, SCANNER_INGEST_CONTRACT_VERSION)

        status = str(body.get("status", ""))
        return PushOutcome(
            action=action_for_status(status), status=status,
            document_id=body.get("document_id"),
            detail=str(body.get("detail", "")), http_status=200,
        )

    async def health(self) -> bool:
        """Backend liveness probe. Never raises. 401/403 reads as unhealthy —
        that needs an operator, not an automatic retry."""
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=self._verify) as client:
                resp = await client.get(f"{self._base}{HEALTH_PATH}", headers=self._headers)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
