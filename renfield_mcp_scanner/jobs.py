"""Scan jobs: `scan_document` returns at once, the scan runs in the background.

Why this exists. Feeding, correcting, OCRing and pushing a stack takes from half
a minute to many minutes, and it used to happen INSIDE the MCP tool call. That
call sat in a chat turn: the user watched a spinner, the backend's 30s call
timeout reported scans as FAILED that had in fact been filed (2026-09-14), and a
longer timeout only moved the problem — a refresh or reconnect during the call
tore it down and the automatic retry found an empty feeder.

The shape now: the tool call starts a job and answers with a `job_id`. When the
job ends, the scanner tells the instance that ASKED — an event, never a poll —
over its existing push credentials. A job's record lives on disk next to the
staging area, so a restart neither loses the outcome nor the pending event.

Only one scan runs at a time. There is one feeder; a second request while it is
busy gets the running job's id instead of a second, empty scan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import Config, MissingTokenError, ScanTarget
from .contract import (
    JOB_EVENT_CONTRACT_HEADER,
    JOB_EVENT_PATH,
    SCANNER_JOB_EVENT_CONTRACT_VERSION,
)

logger = logging.getLogger("renfield-mcp-scanner.jobs")

RUNNING = "running"
DONE = "done"
UNROUTED = "unrouted"
FAILED = "failed"
INTERRUPTED = "interrupted"
TERMINAL = frozenset({DONE, UNROUTED, FAILED, INTERRUPTED})

# Event delivery state of a finished job.
EVENT_PENDING = "pending"
EVENT_RETRY = "retry"
EVENT_DELIVERED = "delivered"
EVENT_FATAL = "fatal"
EVENT_NO_ROUTE = "no_route"
EVENT_GAVE_UP = "gave_up"
_EVENT_OPEN = frozenset({EVENT_PENDING, EVENT_RETRY})

_JOB_ID = re.compile(r"[0-9a-f]{32}")


def outcome_status(result: dict) -> str:
    """Map a `tools.scan_document` result onto a job status.

    A split stack can file some pieces and leave others without a destination —
    that is not a failure, it is waiting for a person, same as a single scan that
    could not be routed."""
    if result.get("ok"):
        return UNROUTED if result.get("routed") is False else DONE
    if result.get("skipped"):
        return UNROUTED
    return FAILED


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobStore:
    """One JSON file per job under ``<staging>/jobs``. Private: 0700."""

    def __init__(self, staging_root: Path):
        self.dir = Path(staging_root).expanduser() / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)

    @staticmethod
    def valid_id(job_id: str) -> bool:
        # The id becomes a file name: anything but our own format is refused, so
        # a caller-supplied id can never walk out of the directory.
        return bool(_JOB_ID.fullmatch(job_id or ""))

    def save(self, job: dict) -> None:
        path = self.dir / f"{job['job_id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=2))
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written record

    def load(self, job_id: str) -> dict | None:
        if not self.valid_id(job_id):
            return None
        path = self.dir / f"{job_id}.json"
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def all(self) -> list[dict]:
        jobs = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                jobs.append(json.loads(path.read_text()))
            except ValueError:
                logger.warning("unreadable job record %s", path.name)
        return jobs

    def purge_settled(self, retention_days: int) -> int:
        """Drop finished jobs whose event is no longer open, past retention."""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        for job in self.all():
            if job.get("status") not in TERMINAL:
                continue
            if job.get("event", {}).get("state") in _EVENT_OPEN:
                continue
            finished = job.get("finished_at")
            if finished and datetime.fromisoformat(finished) < cutoff:
                (self.dir / f"{job['job_id']}.json").unlink(missing_ok=True)
                removed += 1
        return removed


class JobEventNotifier:
    """Sends a finished job's outcome to the instance that requested it.

    The return path is the caller's TARGET (base_url + push token), so no new
    credential exists. Delivery retries with capped exponential backoff — that is
    the sender retrying its own message, not anyone polling for state.
    """

    def __init__(self, config: Config, *, base_delay: float = 2.0, max_delay: float = 300.0,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient):
        self._config = config
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._client_factory = client_factory

    def target_for(self, caller: str | None) -> ScanTarget | None:
        target_id = self._config.caller_targets.get((caller or "").lower())
        return self._config.target_by_id(target_id) if target_id else None

    @staticmethod
    def payload(job: dict) -> dict:
        # Routing and provenance only — ids, counts, reasons. Never page content.
        return {
            "contract_version": SCANNER_JOB_EVENT_CONTRACT_VERSION,
            "job_id": job["job_id"],
            "status": job["status"],
            "title": job.get("title") or "",
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "result": job.get("result") or {},
        }

    async def send_once(self, job: dict, target: ScanTarget) -> str:
        try:
            token = target.token()
        except MissingTokenError as exc:
            logger.error("job %s: %s", job["job_id"], exc)
            return EVENT_FATAL
        headers = {"Authorization": f"Bearer {token}",
                   JOB_EVENT_CONTRACT_HEADER: SCANNER_JOB_EVENT_CONTRACT_VERSION}
        try:
            async with self._client_factory(
                timeout=30.0, verify=target.ca_bundle or True
            ) as client:
                resp = await client.post(f"{target.base_url.rstrip('/')}{JOB_EVENT_PATH}",
                                         headers=headers, json=self.payload(job))
        except httpx.HTTPError as exc:
            logger.info("job %s: event transport error: %s", job["job_id"], exc)
            return EVENT_RETRY
        if 200 <= resp.status_code < 300:
            return EVENT_DELIVERED
        if resp.status_code in (401, 403, 404):
            # A bad token, or an instance without the route (older backend,
            # feature off): retrying cannot fix either, it only hammers.
            logger.error("job %s: event rejected with HTTP %s — not retrying",
                         job["job_id"], resp.status_code)
            return EVENT_FATAL
        logger.info("job %s: event not accepted (HTTP %s)", job["job_id"], resp.status_code)
        return EVENT_RETRY

    async def deliver(self, job: dict, store: JobStore) -> None:
        event = job.setdefault("event", {"state": EVENT_PENDING, "attempts": 0})
        target = self.target_for(job.get("caller"))
        if target is None:
            event["state"] = EVENT_NO_ROUTE
            store.save(job)
            logger.warning("job %s: caller %r has no SCANNER_CALLER_TARGET_* mapping — "
                           "outcome %s is recorded but nobody is told",
                           job["job_id"], job.get("caller"), job["status"])
            return
        delay = self._base_delay
        while event["attempts"] < self._config.job_event_max_attempts:
            event["attempts"] += 1
            state = await self.send_once(job, target)
            event["state"] = state
            store.save(job)
            if state != EVENT_RETRY:
                return
            await self._sleep(delay)
            delay = min(delay * 2, self._max_delay)
        event["state"] = EVENT_GAVE_UP
        store.save(job)
        logger.error("job %s: completion event not delivered after %d attempts",
                     job["job_id"], event["attempts"])


class JobManager:
    def __init__(self, store: JobStore,
                 run_scan: Callable[[str, str], Awaitable[dict]],
                 notifier: JobEventNotifier):
        self._store = store
        self._run_scan = run_scan
        self._notifier = notifier
        self._lock = asyncio.Lock()
        self._active: str | None = None
        # Strong references: a bare create_task can be garbage-collected mid-run.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self, *, caller: str | None, target: str, title: str) -> dict:
        async with self._lock:
            if self._active is not None:
                return {"ok": False, "busy": True, "job_id": self._active,
                        "error": "A scan is already running. Wait for it to finish "
                                 "before starting another — there is only one feeder."}
            job = {
                "job_id": uuid.uuid4().hex, "status": RUNNING,
                "caller": caller, "target": target or "", "title": title or "",
                "started_at": _now(), "finished_at": None, "result": None,
                "event": {"state": EVENT_PENDING, "attempts": 0},
            }
            self._store.save(job)
            self._active = job["job_id"]
        self._spawn(self._run(job))
        logger.info("job %s started for caller %r", job["job_id"], caller)
        return {"ok": True, "job_id": job["job_id"], "status": RUNNING}

    async def _run(self, job: dict) -> None:
        try:
            try:
                result = await self._run_scan(job["target"], job["title"])
                status = outcome_status(result)
            except Exception as exc:  # noqa: BLE001 - a crash must still end the job
                logger.exception("job %s crashed", job["job_id"])
                result, status = {"ok": False, "error": f"scan crashed: {exc}"}, FAILED
            job.update(status=status, result=result, finished_at=_now())
            self._store.save(job)
        finally:
            # Release the feeder BEFORE the event is sent: a slow or unreachable
            # instance must not block the next scan.
            self._active = None
        logger.info("job %s finished: %s", job["job_id"], job["status"])
        await self._notifier.deliver(job, self._store)

    def status(self, job_id: str) -> dict:
        job = self._store.load(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job {job_id!r}"}
        return {"ok": True, "job_id": job["job_id"], "status": job["status"],
                "title": job.get("title") or "", "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"), "result": job.get("result")}

    async def recover(self) -> dict:
        """Settle what a restart left behind.

        A job still RUNNING was cut off mid-scan: its pages may sit in staging,
        but the outcome is unknown, so it is closed as INTERRUPTED and reported.
        A finished job whose event never got through is sent again."""
        interrupted = resent = 0
        for job in self._store.all():
            if job.get("status") == RUNNING:
                job.update(status=INTERRUPTED, finished_at=_now(), result={
                    "ok": False,
                    "error": "The scanner service restarted while this scan was running. "
                             "Pages may be waiting on the scanner host — see "
                             "list_pending_scans. Do not assume the document was filed.",
                })
                self._store.save(job)
                interrupted += 1
            if job.get("status") in TERMINAL and job.get("event", {}).get("state") in _EVENT_OPEN:
                self._spawn(self._notifier.deliver(job, self._store))
                resent += 1
        if interrupted or resent:
            logger.info("job recovery: %d interrupted, %d event(s) re-sent", interrupted, resent)
        return {"interrupted": interrupted, "resent": resent}

    async def wait_idle(self) -> None:
        """Test/shutdown helper: wait for all running work to settle."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
