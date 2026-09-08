"""L3: suggest a destination from the document's own text.

Mirrors the pattern of Renfield's `services/simba_classify.py` — one strict-JSON
call, the answer validated against an ALLOWLIST, best-effort throughout — rather
than sharing its code, which lives in the backend and cannot be imported here.
The confidence field follows `services/pdf_split_detector.py`.

One difference is deliberate and shapes every choice below. simba_classify
PREFILLS A PICKER: a wrong suggestion is corrected by a human before anything
happens. This classifier, above its threshold, FILES A DOCUMENT ACROSS A TRUST
BOUNDARY with nobody looking. Same mechanism, very unequal consequences — so:

  * the threshold is far stricter than a suggestion would need,
  * the model can only ever return a CONFIGURED target id (allowlist), and
  * every failure — unreachable model, unparseable answer, unknown id, low
    confidence — resolves to "undecided", never to a guess.

The prompt is built from the targets' own `description` fields. This module has
no notion of what any target is, so adding one is configuration.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.classifier")

# Enough context to recognise an addressee and a letterhead; keeps it cheap.
_MAX_TEXT_CHARS = 4000


@dataclass(frozen=True)
class Classification:
    target_id: str
    confidence: float
    evidence: str


def extract_text(pdf: Path, max_pages: int = 2) -> str:
    """Text of the first pages. The PDF already carries an OCR layer.

    The ADDRESSEE is the signal that matters, and it is on page one — the sender
    is not: the same insurer or bank writes to every one of these instances.
    """
    try:
        out = subprocess.run(
            ["pdftotext", "-f", "1", "-l", str(max_pages), str(pdf), "-"],
            capture_output=True, timeout=30,
        )
        return out.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("text extraction failed for %s: %s", pdf.name, exc)
        return ""


def _parse_json(raw: str) -> dict | None:
    """Tolerant of a fenced block or prose around the object; strict about the
    result being an object."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = re.sub(r"^(json)\s*", "", raw, flags=re.I).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except (ValueError, TypeError):
                return None
    return None


def _match_target(value: object, allowed: list[str]) -> str | None:
    """Case-insensitive EXACT match against the configured ids.

    The allowlist is the load-bearing part: a model that invents a plausible id
    must not be able to route anywhere. Anything unmatched is a miss, not a
    near-miss to be resolved generously.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    for opt in allowed:
        if opt.lower() == v:
            return opt
    return None


def _prompt(targets, text: str) -> tuple[str, str]:
    listing = "\n".join(f"- {t.id}: {t.description.strip() or t.label}" for t in targets)
    system = (
        "Du ordnest ein eingescanntes Dokument GENAU EINEM Ziel zu. Waehle "
        "AUSSCHLIESSLICH eine der vorgegebenen IDs (exakte Schreibweise). "
        'Antworte nur mit JSON: {"target": "...", "confidence": 0.0, "evidence": "..."} '
        "Das wichtigste Merkmal ist der EMPFAENGER (an wen ist das Dokument "
        "adressiert), nicht der Absender — derselbe Absender schreibt an mehrere "
        "Ziele. confidence ist deine Sicherheit von 0.0 bis 1.0; bei Unsicherheit "
        "gib einen NIEDRIGEN Wert an, statt zu raten. evidence ist die kurze "
        "Textstelle, auf die du dich stuetzt."
    )
    user = (
        f"Moegliche Ziele:\n{listing}\n\n"
        f"Dokumentinhalt (Auszug):\n{text[:_MAX_TEXT_CHARS]}\n\nJSON:"
    )
    return system, user


async def classify(
    text: str, targets, *, url: str, model: str, timeout: float = 60.0
) -> Classification | None:
    """Suggest a target, or None. NEVER raises — a miss means 'undecided'."""
    text = (text or "").strip()
    if not text or not targets or not model:
        return None
    allowed = [t.id for t in targets]

    system, user = _prompt(targets, text)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "temperature": 0.1,
                },
            )
        if resp.status_code != 200:
            logger.warning("classifier HTTP %s", resp.status_code)
            return None
        raw = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - a miss must never break a scan
        logger.warning("classifier unavailable: %s", exc)
        return None

    payload = _parse_json(raw or "")
    if not isinstance(payload, dict):
        logger.warning("classifier returned unparseable output")
        return None

    target = _match_target(payload.get("target"), allowed)
    if not target:
        # An id outside the allowlist is a MISS, not something to reconcile.
        logger.warning("classifier proposed an unknown target %r", payload.get("target"))
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    evidence = str(payload.get("evidence") or "").strip()[:300]
    return Classification(target_id=target, confidence=confidence, evidence=evidence)
