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
import random
import re
import tempfile
import time
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
# Re-sent after a restart: still open, or given up during an outage that has
# since ended — a restart is the cheapest moment to try once more.
_EVENT_RESEND_ON_RESTART = _EVENT_OPEN | {EVENT_GAVE_UP}

# Answers that say "not now" rather than "no": the sender backs off, and honours
# the receiver's Retry-After when it names one.
_RETRY_AFTER_CODES = frozenset({429, 503})

_JOB_ID = re.compile(r"[0-9a-f]{32}")
# A temp file this old is the leftover of a crash mid-write, never a live write.
_STALE_TMP_SECONDS = 3600


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


def public_view(job: dict) -> dict:
    """What leaves this host about a job — the status answer and the completion
    event share it, so the two cannot drift apart."""
    return {"job_id": job["job_id"], "status": job["status"],
            "title": job.get("title") or "", "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"), "result": job.get("result") or {}}


def _equal_jitter(delay: float) -> float:
    """Somewhere in the upper half of the delay. After an outage every open event
    would otherwise retry in lockstep and hit the recovering instance together."""
    return random.uniform(delay / 2, delay)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None  # the HTTP-date form is not worth a parser here
    return seconds if seconds >= 0 else None


class JobStore:
    """One JSON file per job under ``<staging>/jobs``. Private: 0700.

    Every disk access runs in a worker thread: the MCP server answers tool calls
    and the backend's `list_tools` refresh from the same event loop, and a slow
    disk (or a sleeping one waking up) must not stall them — a stalled loop is
    exactly what once made the backend declare the scanner dead.

    Writes are atomic (a unique temp file, fsync, rename) and serialized: the
    record is serialized on the loop at the moment `save` is called, and writes
    run one at a time in call order, so a slow earlier write can never land
    after — and overwrite — a newer state of the same job.
    """

    def __init__(self, staging_root: Path):
        self.dir = Path(staging_root).expanduser() / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        self._write_lock = asyncio.Lock()

    @staticmethod
    def valid_id(job_id: str) -> bool:
        # The id becomes a file name: anything but our own format is refused, so
        # a caller-supplied id can never walk out of the directory.
        return bool(_JOB_ID.fullmatch(job_id or ""))

    def _path(self, job_id: str) -> Path:
        if not self.valid_id(job_id):
            raise ValueError(f"not a job id: {job_id!r}")
        return self.dir / f"{job_id}.json"

    # --- async API -------------------------------------------------------------

    async def save(self, job: dict) -> None:
        path = self._path(job["job_id"])
        data = json.dumps(job, indent=2)  # snapshot NOW, not when the thread runs
        async with self._write_lock:
            await asyncio.to_thread(self._write_atomic, path, data)

    async def load(self, job_id: str) -> dict | None:
        if not self.valid_id(job_id):
            return None
        return await asyncio.to_thread(self._load_sync, job_id)

    async def all(self) -> list[dict]:
        return await asyncio.to_thread(self._all_sync)

    async def purge_settled(self, retention_days: int) -> int:
        """Drop finished jobs whose event is no longer open, past retention."""
        if retention_days <= 0:
            return 0
        # Under the write lock: a purge must not unlink a record between another
        # coroutine's snapshot and its rename.
        async with self._write_lock:
            return await asyncio.to_thread(self._purge_sync, retention_days)

    # --- blocking helpers (worker thread only) -----------------------------------

    def _write_atomic(self, path: Path, data: str) -> None:
        # A UNIQUE temp name: a fixed `<id>.tmp` would let two writers share one
        # file. It never matches `*.json`, so a reader cannot pick up a half file.
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())  # the rename must not outrun the data on power loss
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _load_sync(self, job_id: str) -> dict | None:
        try:
            return json.loads(self._path(job_id).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _all_sync(self) -> list[dict]:
        jobs = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                jobs.append(json.loads(path.read_text()))
            except FileNotFoundError:
                continue  # purged between the listing and the read
            except ValueError:
                logger.warning("unreadable job record %s", path.name)
        return jobs

    def _purge_sync(self, retention_days: int) -> int:
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        for job in self._all_sync():
            if job.get("status") not in TERMINAL:
                continue
            if job.get("event", {}).get("state") in _EVENT_OPEN:
                continue
            try:
                finished = datetime.fromisoformat(job.get("finished_at") or "")
            except (TypeError, ValueError):
                # A hand-edited or foreign record must not stop the server from
                # starting (this runs before it binds); it is simply kept.
                logger.warning("job %s has no readable finished_at — not purged",
                               job.get("job_id"))
                continue
            if finished < cutoff:
                self._path(job["job_id"]).unlink(missing_ok=True)
                removed += 1
        stale = time.time() - _STALE_TMP_SECONDS
        for tmp in self.dir.glob(".*.tmp"):
            try:
                if tmp.stat().st_mtime < stale:
                    tmp.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
        return removed


class JobEventNotifier:
    """Sends a finished job's outcome to the instance that requested it.

    The return path is the caller's TARGET (base_url + push token), so no new
    credential exists. Delivery retries with capped, jittered exponential backoff
    for `job_event_retry_hours` — that is the sender retrying its own message, not
    anyone polling for state.

    Sends are bounded per target (`job_event_concurrency`). After a long outage
    every finished job has an open event; unbounded, they would all fire at the
    instance the moment it comes back — the worst moment to be hit by a burst.
    Only the HTTP request holds a slot, never the backoff sleep, and the bound is
    per target so an unreachable instance cannot starve deliveries to another.
    """

    def __init__(self, config: Config, *, base_delay: float = 2.0, max_delay: float = 300.0,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 jitter: Callable[[float], float] = _equal_jitter,
                 client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient):
        self._config = config
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._client_factory = client_factory
        self._concurrency = max(1, int(config.job_event_concurrency))
        self._slots: dict[str, asyncio.Semaphore] = {}

    def target_for(self, caller: str | None) -> ScanTarget | None:
        target_id = self._config.caller_targets.get((caller or "").lower())
        return self._config.target_by_id(target_id) if target_id else None

    @staticmethod
    def payload(job: dict) -> dict:
        # Routing and provenance only — ids, counts, reasons. Never page content.
        return {"contract_version": SCANNER_JOB_EVENT_CONTRACT_VERSION, **public_view(job)}

    def _slot(self, target: ScanTarget) -> asyncio.Semaphore:
        slot = self._slots.get(target.id)
        if slot is None:
            slot = self._slots[target.id] = asyncio.Semaphore(self._concurrency)
        return slot

    async def send_once(self, job: dict, target: ScanTarget) -> tuple[str, float | None]:
        """One attempt. Returns the event state and, for a "not now" answer, the
        receiver's Retry-After in seconds (None when it named none)."""
        try:
            token = target.token()
        except MissingTokenError as exc:
            logger.error("job %s: %s", job["job_id"], exc)
            return EVENT_FATAL, None
        headers = {"Authorization": f"Bearer {token}",
                   JOB_EVENT_CONTRACT_HEADER: SCANNER_JOB_EVENT_CONTRACT_VERSION}
        try:
            async with self._slot(target), self._client_factory(
                timeout=30.0, verify=target.ca_bundle or True
            ) as client:
                resp = await client.post(f"{target.base_url.rstrip('/')}{JOB_EVENT_PATH}",
                                         headers=headers, json=self.payload(job))
        except httpx.HTTPError as exc:
            logger.info("job %s: event transport error: %s", job["job_id"], exc)
            return EVENT_RETRY, None
        if 200 <= resp.status_code < 300:
            return EVENT_DELIVERED, None
        if resp.status_code in (401, 403, 404):
            # A bad token, or an instance without the route (older backend,
            # feature off): retrying cannot fix either, it only hammers.
            logger.error("job %s: event rejected with HTTP %s — not retrying",
                         job["job_id"], resp.status_code)
            return EVENT_FATAL, None
        logger.info("job %s: event not accepted (HTTP %s)", job["job_id"], resp.status_code)
        retry_after = (_retry_after_seconds(resp)
                       if resp.status_code in _RETRY_AFTER_CODES else None)
        return EVENT_RETRY, retry_after

    async def deliver(self, job: dict, store: JobStore) -> None:
        event = job.setdefault("event", {"state": EVENT_PENDING, "attempts": 0})
        target = self.target_for(job.get("caller"))
        if target is None:
            event["state"] = EVENT_NO_ROUTE
            await store.save(job)
            logger.warning("job %s: caller %r has no SCANNER_CALLER_TARGET_* mapping — "
                           "outcome %s is recorded but nobody is told",
                           job["job_id"], job.get("caller"), job["status"])
            return
        delay = self._base_delay
        started = self._clock()
        horizon = self._config.job_event_retry_hours * 3600
        while True:
            event["attempts"] += 1
            state, retry_after = await self.send_once(job, target)
            event["state"] = state
            await store.save(job)
            if state != EVENT_RETRY:
                return
            if self._clock() - started >= horizon:
                break
            wait = self._jitter(delay)
            if retry_after is not None:
                # The receiver knows its own recovery better than our curve does,
                # but a hostile or broken header must not park the event for days.
                wait = max(wait, min(retry_after, self._max_delay))
            await self._sleep(wait)
            delay = min(delay * 2, self._max_delay)
        event["state"] = EVENT_GAVE_UP
        await store.save(job)
        logger.error("job %s: completion event not delivered within %.1f h (%d attempts)",
                     job["job_id"], self._config.job_event_retry_hours, event["attempts"])


class JobManager:
    def __init__(self, store: JobStore,
                 run_scan: Callable[[str, str], Awaitable[dict]],
                 notifier: JobEventNotifier,
                 *, retention_days: int = 0):
        self._store = store
        self._run_scan = run_scan
        self._notifier = notifier
        self._retention_days = retention_days
        self._lock = asyncio.Lock()
        self._active: str | None = None
        self._active_caller: str | None = None
        # Strong references: a bare create_task can be garbage-collected mid-run.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self, *, caller: str | None, target: str, title: str) -> dict:
        async with self._lock:
            if self._active is not None:
                # `message`, not `error`: busy is an expected answer, and an
                # `error` field reads as a failed tool call — which invites the
                # agent to retry exactly what it was told not to.
                busy = {"ok": False, "busy": True,
                        "message": "A scan is already running. Wait for it to finish "
                                   "before starting another — there is only one feeder."}
                # Only the caller that started it learns the running job's id —
                # another instance must not be able to read that job.
                if self._active_caller == caller:
                    busy["job_id"] = self._active
                return busy
            job = {
                "job_id": uuid.uuid4().hex, "status": RUNNING,
                "caller": caller, "target": target or "", "title": title or "",
                "started_at": _now(), "finished_at": None, "result": None,
                "event": {"state": EVENT_PENDING, "attempts": 0},
            }
            await self._store.save(job)
            self._active = job["job_id"]
            self._active_caller = caller
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
                result = {"ok": False, "error": f"scan crashed: {exc}", "error_code": "crashed"}
                status = FAILED
            job.update(status=status, result=result, finished_at=_now())
            try:
                await self._store.save(job)
            except OSError:
                # The outcome is still in memory and is still reported; only its
                # durability across a restart is lost. Never keep the feeder.
                logger.exception("job %s: could not record the outcome on disk", job["job_id"])
        finally:
            # Release the feeder BEFORE the event is sent: a slow or unreachable
            # instance must not block the next scan.
            self._active = None
            self._active_caller = None
        logger.info("job %s finished: %s", job["job_id"], job["status"])
        await self._deliver_and_tidy(job)

    async def _deliver_and_tidy(self, job: dict) -> None:
        """Send the event, then drop records that have aged out.

        Cleanup is tied to THIS moment — a job's event settling — rather than to a
        timer: the job directory only ever grows when a job finishes, so sweeping
        right then bounds it to the retention window (plus open events) with no
        periodic wake-up on the many days nobody scans. Startup sweeps once more
        for records left from before a restart."""
        try:
            await self._notifier.deliver(job, self._store)
        finally:
            try:
                purged = await self._store.purge_settled(self._retention_days)
                if purged:
                    logger.info("purged %d settled job record(s)", purged)
            except OSError:
                logger.exception("job record cleanup failed")

    async def status(self, job_id: str, caller: str | None = None) -> dict:
        """A job's state — only to the caller that started it. Another caller
        gets exactly the reply an id that does not exist would get."""
        job = await self._store.load(job_id)
        if job is None or (caller is not None and job.get("caller") != caller):
            return {"ok": False, "error": f"unknown job {job_id!r}"}
        return {"ok": True, **public_view(job)}

    async def recover(self) -> dict:
        """Settle what a restart left behind.

        A job still RUNNING was cut off mid-scan: its pages may sit in staging,
        but the outcome is unknown, so it is closed as INTERRUPTED and reported.
        A finished job whose event never got through is sent again."""
        interrupted = resent = 0
        for job in await self._store.all():
            if job.get("status") == RUNNING:
                job.update(status=INTERRUPTED, finished_at=_now(), result={
                    "ok": False,
                    "error": "The scanner service restarted while this scan was running. "
                             "Pages may be waiting on the scanner host — see "
                             "list_pending_scans. Do not assume the document was filed.",
                })
                interrupted += 1
            if (job.get("status") in TERMINAL
                    and job.get("event", {}).get("state") in _EVENT_RESEND_ON_RESTART):
                # A fresh attempt budget: the budget is per run, and an event
                # that had used it up before the restart would otherwise get
                # not a single send now. Marked open ON DISK before the send, so
                # a cleanup sweep in the meantime keeps it.
                job["event"] = {"state": EVENT_PENDING, "attempts": 0}
                await self._store.save(job)
                self._spawn(self._deliver_and_tidy(job))
                resent += 1
            elif job.get("status") == INTERRUPTED:
                await self._store.save(job)
        if interrupted or resent:
            logger.info("job recovery: %d interrupted, %d event(s) re-sent", interrupted, resent)
        return {"interrupted": interrupted, "resent": resent}

    async def wait_idle(self) -> None:
        """Test/shutdown helper: wait for all running work to settle."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
