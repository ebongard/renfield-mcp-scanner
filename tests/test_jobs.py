"""Scan jobs: immediate return, single flight, completion events, restart recovery.

The failures these pin, all from 2026-09-14: a scan reported FAILED while it had
been filed (the call outlived the backend's timeout); a longer timeout letting a
refresh tear the call down and an automatic retry scan an empty feeder; and the
server's event loop frozen for 12s by OCR, which is what made the refresh give up.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from renfield_mcp_scanner import jobs as j
from renfield_mcp_scanner.config import Config, ScanTarget


def _cfg(tmp_path, caller_targets=None, retry_hours=24.0):
    return Config(
        targets=[ScanTarget(id="household", label="Haushalt", base_url="https://hh:8443",
                            token_env="TOK_HH", scan_profile_id="p")],
        staging_dir=tmp_path / "st",
        caller_targets=caller_targets if caller_targets is not None else {"household": "household"},
        job_event_retry_hours=retry_hours,
    )


class _Notifier:
    def __init__(self):
        self.delivered = []

    async def deliver(self, job, store):
        self.delivered.append(job["job_id"])
        job["event"] = {"state": j.EVENT_DELIVERED, "attempts": 1}
        store.save(job)


# --- outcome mapping ---------------------------------------------------------

@pytest.mark.parametrize("result, status", [
    ({"ok": True, "routed": True}, j.DONE),
    ({"ok": True, "routed": False}, j.UNROUTED),
    ({"ok": False, "routed": True, "skipped": [{"segment": 0}], "documents": []}, j.UNROUTED),
    ({"ok": False, "error": "no pages scanned"}, j.FAILED),
])
def test_outcome_status(result, status):
    assert j.outcome_status(result) == status


# --- store -------------------------------------------------------------------

def test_store_refuses_ids_that_are_not_ours(tmp_path):
    store = j.JobStore(tmp_path)
    assert store.load("../../etc/passwd") is None
    assert store.load("") is None
    assert not j.JobStore.valid_id("ABC")


def test_store_round_trip_is_private(tmp_path):
    store = j.JobStore(tmp_path)
    job = {"job_id": "a" * 32, "status": j.RUNNING}
    store.save(job)
    assert store.load("a" * 32) == job
    assert oct(store.dir.stat().st_mode)[-3:] == "700"


# --- manager -----------------------------------------------------------------

async def test_start_returns_before_the_scan_finishes(tmp_path):
    release = asyncio.Event()

    async def run_scan(target, title):
        await release.wait()
        return {"ok": True, "routed": True, "renfield_document_id": 613}

    notifier = _Notifier()
    manager = j.JobManager(j.JobStore(tmp_path), run_scan, notifier)

    started = await asyncio.wait_for(manager.start(caller="household", target="", title="T"), 1)
    assert started["ok"] and started["status"] == j.RUNNING
    assert manager.status(started["job_id"])["status"] == j.RUNNING

    release.set()
    await manager.wait_idle()
    finished = manager.status(started["job_id"])
    assert finished["status"] == j.DONE
    assert finished["result"]["renfield_document_id"] == 613
    assert notifier.delivered == [started["job_id"]]


async def test_second_start_while_busy_names_the_running_job(tmp_path):
    """One feeder: a retry or double request must not start an empty second scan."""
    release = asyncio.Event()
    calls = []

    async def run_scan(target, title):
        calls.append(1)
        await release.wait()
        return {"ok": True, "routed": True}

    manager = j.JobManager(j.JobStore(tmp_path), run_scan, _Notifier())
    first = await manager.start(caller="household", target="", title="")
    second = await manager.start(caller="household", target="", title="")

    assert second["ok"] is False and second["busy"] is True
    assert second["job_id"] == first["job_id"]
    release.set()
    await manager.wait_idle()
    assert calls == [1]


async def test_another_caller_learns_nothing_about_the_running_job(tmp_path):
    """Three instances share one scanner: xidra must not read household's scan."""
    release = asyncio.Event()

    async def run_scan(target, title):
        await release.wait()
        return {"ok": True, "routed": True, "renfield_document_id": 9}

    manager = j.JobManager(j.JobStore(tmp_path), run_scan, _Notifier())
    mine = await manager.start(caller="household", target="", title="Steuer")
    theirs = await manager.start(caller="xidra", target="", title="")

    assert theirs["busy"] is True and "job_id" not in theirs
    assert manager.status(mine["job_id"], caller="xidra") == {
        "ok": False, "error": f"unknown job {mine['job_id']!r}"}
    assert manager.status(mine["job_id"], caller="household")["ok"] is True
    release.set()
    await manager.wait_idle()


def test_purge_survives_an_unreadable_timestamp(tmp_path):
    """purge runs before the server binds — one odd record must not stop it."""
    store = j.JobStore(tmp_path)
    store.save({"job_id": "7" * 32, "status": j.DONE, "finished_at": "yesterday",
                "event": {"state": j.EVENT_DELIVERED}})
    store.save({"job_id": "8" * 32, "status": j.DONE,
                "event": {"state": j.EVENT_DELIVERED}})
    assert store.purge_settled(30) == 0
    assert store.purge_settled(0) == 0


async def test_recovery_resends_an_event_that_had_given_up(tmp_path, monkeypatch):
    """An outage longer than the horizon ended; the restart tries once more."""
    monkeypatch.setenv("TOK_HH", "secret")
    store = j.JobStore(tmp_path)
    store.save({"job_id": "9" * 32, "status": j.DONE, "caller": "household",
                "result": {"ok": True}, "event": {"state": j.EVENT_GAVE_UP, "attempts": 40}})
    seen = []
    notifier, _ = _notifier(tmp_path, [200], seen)
    manager = j.JobManager(store, run_scan=None, notifier=notifier)

    await manager.recover()
    await manager.wait_idle()

    assert len(seen) == 1
    assert store.load("9" * 32)["event"] == {"state": j.EVENT_DELIVERED, "attempts": 1}


async def test_feeder_is_free_again_after_a_crash(tmp_path):
    async def boom(target, title):
        raise RuntimeError("sane died")

    notifier = _Notifier()
    manager = j.JobManager(j.JobStore(tmp_path), boom, notifier)
    job = await manager.start(caller="household", target="", title="")
    await manager.wait_idle()

    assert manager.status(job["job_id"])["status"] == j.FAILED
    assert notifier.delivered == [job["job_id"]], "a crash must still be reported"
    again = await manager.start(caller="household", target="", title="")
    assert again["ok"]
    await manager.wait_idle()


async def test_feeder_is_released_before_the_event_is_sent(tmp_path):
    """A slow or unreachable instance must not block the next scan."""
    event_gate = asyncio.Event()

    class _SlowNotifier:
        async def deliver(self, job, store):
            await event_gate.wait()

    async def run_scan(target, title):
        return {"ok": True, "routed": True}

    manager = j.JobManager(j.JobStore(tmp_path), run_scan, _SlowNotifier())
    await manager.start(caller="household", target="", title="")
    for _ in range(20):
        await asyncio.sleep(0)
    second = await manager.start(caller="household", target="", title="")
    assert second["ok"], "the feeder stayed locked while the event was pending"
    event_gate.set()
    await manager.wait_idle()


async def test_recover_closes_cut_off_jobs_and_resends_open_events(tmp_path):
    store = j.JobStore(tmp_path)
    store.save({"job_id": "1" * 32, "status": j.RUNNING, "caller": "household",
                "event": {"state": j.EVENT_PENDING, "attempts": 0}})
    store.save({"job_id": "2" * 32, "status": j.DONE, "caller": "household",
                "result": {"ok": True}, "event": {"state": j.EVENT_RETRY, "attempts": 4}})
    store.save({"job_id": "3" * 32, "status": j.DONE, "caller": "household",
                "result": {"ok": True}, "event": {"state": j.EVENT_DELIVERED, "attempts": 1}})
    notifier = _Notifier()
    manager = j.JobManager(store, run_scan=None, notifier=notifier)

    summary = await manager.recover()
    await manager.wait_idle()

    assert summary == {"interrupted": 1, "resent": 2}
    cut_off = store.load("1" * 32)
    assert cut_off["status"] == j.INTERRUPTED
    assert "Do not assume the document was filed" in cut_off["result"]["error"]
    assert sorted(notifier.delivered) == ["1" * 32, "2" * 32]


def test_purge_keeps_open_events(tmp_path):
    store = j.JobStore(tmp_path)
    old = "2000-01-01T00:00:00"
    store.save({"job_id": "4" * 32, "status": j.DONE, "finished_at": old,
                "event": {"state": j.EVENT_DELIVERED}})
    store.save({"job_id": "5" * 32, "status": j.DONE, "finished_at": old,
                "event": {"state": j.EVENT_RETRY}})
    assert store.purge_settled(30) == 1
    assert store.load("4" * 32) is None and store.load("5" * 32) is not None


# --- event notifier ------------------------------------------------------------

def _job(tmp_path, caller="household"):
    store = j.JobStore(tmp_path)
    job = {"job_id": "6" * 32, "status": j.DONE, "caller": caller, "title": "T",
           "result": {"ok": True, "renfield_document_id": 613},
           "event": {"state": j.EVENT_PENDING, "attempts": 0}}
    store.save(job)
    return store, job


def _notifier(tmp_path, replies, seen, **cfg):
    replies = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(reply)

    transport = httpx.MockTransport(handler)
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    notifier = j.JobEventNotifier(
        _cfg(tmp_path, **cfg), sleep=no_sleep,
        clock=lambda: float(sum(sleeps)),  # time passes only by backing off
        client_factory=lambda **kw: httpx.AsyncClient(transport=transport, **kw))
    return notifier, sleeps


async def test_event_is_posted_to_the_callers_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_HH", "secret")
    store, job = _job(tmp_path)
    seen = []
    notifier, _ = _notifier(tmp_path, [200], seen)

    await notifier.deliver(job, store)

    assert store.load(job["job_id"])["event"] == {"state": j.EVENT_DELIVERED, "attempts": 1}
    request = seen[0]
    assert str(request.url) == "https://hh:8443/api/scanner/job-event"
    assert request.headers["authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert body["job_id"] == job["job_id"] and body["status"] == j.DONE
    assert body["result"]["renfield_document_id"] == 613


async def test_event_retries_with_backoff_then_delivers(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK_HH", "secret")
    store, job = _job(tmp_path)
    seen = []
    notifier, sleeps = _notifier(tmp_path, [503, httpx.ConnectError("down"), 200], seen)

    await notifier.deliver(job, store)

    assert store.load(job["job_id"])["event"] == {"state": j.EVENT_DELIVERED, "attempts": 3}
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize("code", [401, 403, 404])
async def test_event_stops_on_answers_a_retry_cannot_fix(tmp_path, monkeypatch, code):
    monkeypatch.setenv("TOK_HH", "secret")
    store, job = _job(tmp_path)
    seen = []
    notifier, sleeps = _notifier(tmp_path, [code], seen)

    await notifier.deliver(job, store)

    assert store.load(job["job_id"])["event"]["state"] == j.EVENT_FATAL
    assert len(seen) == 1 and sleeps == []


async def test_event_gives_up_only_after_the_retry_horizon(tmp_path, monkeypatch):
    """Time-bound, not count-bound: 12 attempts gave up after ~28 min, shorter
    than an ordinary backend outage. Here the horizon is 3.6 s of backoff."""
    monkeypatch.setenv("TOK_HH", "secret")
    store, job = _job(tmp_path)
    seen = []
    notifier, sleeps = _notifier(tmp_path, [503, 503, 503], seen, retry_hours=0.001)

    await notifier.deliver(job, store)

    assert store.load(job["job_id"])["event"] == {"state": j.EVENT_GAVE_UP, "attempts": 3}
    assert sleeps == [2.0, 4.0]


def test_default_retry_horizon_matches_the_backend_record():
    """Renfield keeps the requester record for 24 h; giving up sooner loses
    outcomes the backend could still have delivered."""
    assert Config(targets=[], staging_dir="/tmp/x").job_event_retry_hours == 24.0


async def test_caller_without_mapping_sends_nothing(tmp_path):
    store, job = _job(tmp_path, caller="stranger")
    seen = []
    notifier, _ = _notifier(tmp_path, [], seen)

    await notifier.deliver(job, store)

    assert store.load(job["job_id"])["event"]["state"] == j.EVENT_NO_ROUTE
    assert seen == []


async def test_missing_push_token_is_fatal_not_a_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("TOK_HH", raising=False)
    store, job = _job(tmp_path)
    seen = []
    notifier, _ = _notifier(tmp_path, [], seen)

    await notifier.deliver(job, store)

    assert store.load(job["job_id"])["event"]["state"] == j.EVENT_FATAL
    assert seen == []


# --- the event loop stays free during OCR ---------------------------------------

async def test_assemble_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """A blocking assemble (img2pdf + ocrmypdf) must not freeze other requests —
    that freeze is what let the backend's refresh declare the scanner dead."""
    from renfield_mcp_scanner.contract import StageAction
    from renfield_mcp_scanner.pusher import PushOutcome
    from renfield_mcp_scanner.staging import Staging
    from renfield_mcp_scanner.tools import _deliver

    monkeypatch.setenv("TOK_HH", "t")

    class _P:
        def __init__(self, *a, **k):
            pass

        async def push(self, *a, **k):
            return PushOutcome(StageAction.DISCARD, status="ingested", document_id=1)

    monkeypatch.setattr("renfield_mcp_scanner.tools.TargetPusher", _P)

    def slow_assemble(pages, stage, config, title=""):
        time.sleep(0.3)
        out = stage / "doc.pdf"
        out.write_bytes(b"%PDF")
        return out

    config = _cfg(tmp_path)
    staging = Staging(config.staging_dir)
    stage = staging.new_stage()
    # Deterministic, not timing-based: the loop must be able to finish other work
    # WHILE assemble is still blocked in its thread.
    assemble_started = threading.Event()
    loop_answered_meanwhile = []

    def blocking_assemble(pages, stage_dir, cfg, title=""):
        assemble_started.set()
        time.sleep(0.3)
        loop_answered_meanwhile.append(bool(answered.is_set()))
        return slow_assemble(pages, stage_dir, cfg, title)

    answered = threading.Event()

    async def other_request():
        while not assemble_started.is_set():
            await asyncio.sleep(0.001)
        answered.set()

    await asyncio.gather(
        _deliver(config, staging, blocking_assemble, stage_dir=stage, pages=[],
                 target=config.targets[0], title=""),
        other_request(),
    )
    assert loop_answered_meanwhile == [True], "event loop was blocked during assemble"


# --- caller extraction -----------------------------------------------------------

def test_caller_comes_from_the_authenticated_request():
    from renfield_mcp_scanner.server import _caller

    ctx = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(caller="xidra"))))
    assert _caller(ctx) == "xidra"


def test_unauthenticated_loopback_is_the_default_caller():
    from renfield_mcp_scanner.server import _caller

    assert _caller(None) == "default"
    no_state = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace())))
    assert _caller(no_state) == "default"
