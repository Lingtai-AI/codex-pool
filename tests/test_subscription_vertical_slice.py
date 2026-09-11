from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from filelock import FileLock

from subs_pool.modules.codex.accounts import AccountStore
from subs_pool.modules.codex.chain import ChainStore
from subs_pool.modules.codex.quota import QuotaResult
from subs_pool.modules.codex.quota_refresh import QuotaRefreshCoordinator
from subs_pool.modules.codex.quota_refresh import refresh_async
from subs_pool.modules.codex.quota_store import ClockGuard, QuotaStateError, QuotaStore, iso
from subs_pool.modules.codex.server import create_app


def _auth(path: Path, *, account_id: str = "acct") -> None:
    path.write_text(
        json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600, "account_id": account_id}),
        encoding="utf-8",
    )


def _clock(start: datetime):
    state = [start]

    def now() -> datetime:
        return state[0]

    def advance(seconds: float) -> None:
        state[0] += timedelta(seconds=seconds)

    return now, advance


def _reader_ok(path: str, **kwargs) -> QuotaResult:
    return QuotaResult(20.0, None, None, None, datetime.now(timezone.utc).isoformat())


def test_home_precedence_and_direct_root_layout(tmp_path, monkeypatch):
    generic = tmp_path / "generic"
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("SUBS_POOL_HOME", str(generic))
    monkeypatch.setenv("CODEX_POOL_HOME", str(explicit))
    store = AccountStore()
    assert store.root == explicit.resolve()
    assert store.path == explicit.resolve() / "pool.json"
    assert not (generic / "codex").exists()
    store.list()
    assert (explicit / "state.lock").is_file()
    assert (explicit / "quota-v1.refresh.lock").is_file()
    assert not (explicit / "accounts").exists()


def test_missing_epoch_is_legacy_compatible_and_legacy_exhaustion_is_ignored(tmp_path):
    auth = tmp_path / "auth.json"
    _auth(auth)
    (tmp_path / "pool.json").write_text(json.dumps({"accounts": {"old": {"auth_path": str(auth), "enabled": True, "weight": 1, "quota_exhausted": True}}}), encoding="utf-8")
    store = AccountStore(tmp_path / "pool.json")
    account = store.get("old")
    assert account.quota_epoch == "legacy-v1"
    assert QuotaStore(tmp_path).view_for(account, QuotaStore(tmp_path).read())["exclusion_reason"] == "never_checked"


def test_quota_sidecar_success_null_secondary_and_stale_history(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    current, advance = _clock(datetime(2026, 9, 11, tzinfo=timezone.utc))

    def reader(path: str, **kwargs) -> QuotaResult:
        return QuotaResult(20.0, None, None, None, iso(current()))

    coordinator = QuotaRefreshCoordinator(accounts, reader=reader, quota_store=QuotaStore(root, now=current), clock=current)
    outcome = coordinator.refresh()
    assert outcome.status == "completed"
    snapshot = QuotaStore(root, now=current).read()
    record = snapshot["accounts"]["a"]
    assert record["status"] == "ok"
    assert record["last_success"]["primary"]["remaining_percent"] == 80.0
    assert record["last_success"]["secondary"]["remaining_percent"] is None
    assert QuotaStore(root, now=current).is_eligible(account, snapshot=snapshot)
    advance(61)
    view = QuotaStore(root, now=current).view_for(account, snapshot)
    assert view["freshness"] == "stale"
    assert view["current"] is None
    assert view["last_success"] is not None
    assert view["eligible"] is False


@pytest.mark.parametrize(
    "secondary",
    [
        {"used_percent": 10, "usedPercent": "bad"},
        {"used_percent": 10, "usedPercent": 20},
        {"used_percent": 10, "window_duration_mins": 60, "limit_window_seconds": 7200},
    ],
)
def test_invalid_secondary_alias_never_commits_or_routes(tmp_path, secondary):
    root = tmp_path / "malformed-alias"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "rate_limit": {"primary_window": {"used_percent": 10}, "secondary_window": secondary}
        })

    store = QuotaStore(root)
    outcome = QuotaRefreshCoordinator(
        accounts, quota_store=store, transport=httpx.MockTransport(handler)
    ).refresh()
    snapshot = store.read()
    record = snapshot["accounts"][account.ref]
    view = store.view_for(account, snapshot)

    assert outcome.ok is False
    assert record["last_success"] is None
    assert record["status"] == "failed"
    assert record["error"]["code"] == "invalid_quota_response"
    assert view["eligible"] is False


@pytest.mark.parametrize(
    "containers",
    [
        {"secondary_window": {"used_percent": 10}, "secondary": {"used_percent": "bad"}},
        {"secondary_window": {"used_percent": 10}, "secondary": {"used_percent": 20}},
        {
            "secondary_window": {"used_percent": 10, "reset_at": 1_700_000_000},
            "secondary": {"used_percent": 10, "reset_at": 1_700_000_100},
        },
        {
            "secondary_window": {"used_percent": 10, "window_duration_mins": 60},
            "secondary": {"used_percent": 10, "window_duration_mins": 120},
        },
    ],
)
def test_invalid_outer_secondary_container_never_commits_or_routes(tmp_path, containers):
    root = tmp_path / "malformed-outer-alias"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "rate_limit": {"primary_window": {"used_percent": 10}, **containers}
        })

    store = QuotaStore(root)
    outcome = QuotaRefreshCoordinator(
        accounts, quota_store=store, transport=httpx.MockTransport(handler)
    ).refresh()
    snapshot = store.read()
    record = snapshot["accounts"][account.ref]
    view = store.view_for(account, snapshot)

    assert outcome.ok is False
    assert record["status"] == "failed"
    assert record["last_success"] is None
    assert record["error"]["code"] == "invalid_quota_response"
    assert view["eligible"] is False


def test_equal_outer_secondary_containers_commit_and_route(tmp_path):
    root = tmp_path / "equal-outer-alias"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "rate_limit": {
                "primary_window": {"used_percent": 10},
                "secondary_window": {"used_percent": 10, "window_duration_mins": 60},
                "secondary": {"usedPercent": 10.0, "limit_window_seconds": 3600},
            }
        })

    store = QuotaStore(root)
    outcome = QuotaRefreshCoordinator(
        accounts, quota_store=store, transport=httpx.MockTransport(handler)
    ).refresh()
    snapshot = store.read()

    assert outcome.ok is True
    assert snapshot["accounts"][account.ref]["last_success"] is not None
    assert store.is_eligible(account, snapshot=snapshot)


def test_failed_refresh_preserves_last_success_and_does_not_fabricate_zero(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    accounts.import_account("a", str(auth))
    current, advance = _clock(datetime(2026, 9, 11, tzinfo=timezone.utc))
    ok = lambda path, **kwargs: QuotaResult(10.0, 30.0, None, None, iso(current()))
    coordinator = QuotaRefreshCoordinator(accounts, reader=ok, quota_store=QuotaStore(root, now=current), clock=current)
    assert coordinator.refresh().status == "completed"
    advance(6)
    failed = lambda path, **kwargs: QuotaResult(None, None, None, None, iso(current()), error="http_status_429")
    coordinator.reader = failed
    outcome = coordinator.refresh()
    assert outcome.status == "partial"
    snapshot = QuotaStore(root, now=current).read()
    record = snapshot["accounts"]["a"]
    assert record["status"] == "failed"
    assert record["last_success"]["primary"]["remaining_percent"] == 90.0
    view = QuotaStore(root, now=current).view_for(accounts.get("a"), snapshot)
    assert view["current"] is None
    assert view["last_success"] is not None


def test_forced_refresh_waits_cooldown_or_marks_old_green_unsatisfied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    current, _advance_wall = _clock(datetime(2026, 9, 11, tzinfo=timezone.utc))
    monotonic = [0.0]

    def sleep(seconds: float) -> None:
        monotonic[0] += seconds

    def reader(path: str, **kwargs) -> QuotaResult:
        return QuotaResult(20.0, None, None, None, iso(current()))

    store = QuotaStore(root, now=current, monotonic=lambda: monotonic[0])
    coordinator = QuotaRefreshCoordinator(
        accounts,
        quota_store=store,
        reader=reader,
        clock=current,
        monotonic=lambda: monotonic[0],
        sleep=sleep,
    )
    assert coordinator.refresh().ok
    waited = coordinator.refresh(timeout_seconds=6)
    assert waited.ok
    assert monotonic[0] >= 5.0

    blocked = coordinator.refresh(timeout_seconds=3)
    assert blocked.status == "unavailable"
    assert blocked.unsatisfied_refs == ("a",)
    snapshot = store.read()
    view = store.rows(accounts.list(), snapshot, now=current(), unsatisfied_refs=set(blocked.unsatisfied_refs))[0]
    assert view["current"] is None
    assert view["eligible"] is False
    assert view["exclusion_reason"] == "refresh_unavailable"
    assert view["last_success"] is not None


def test_refresh_priority_is_epoch_first_then_oldest_source_then_ref(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    accounts = AccountStore(root / "pool.json")
    auth = root / "auth.json"
    _auth(auth)
    for ref in ("c", "a", "b"):
        accounts.import_account(ref, str(auth))
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    store = QuotaStore(root, now=lambda: now)
    for ref, source in (("a", now - timedelta(seconds=10)), ("b", now - timedelta(seconds=1))):
        account = accounts.get(ref)
        store.claim([account], attempt_id=f"seed-{ref}", owner_id="seed", deadline_at=now + timedelta(seconds=20))
        assert store.commit_success(account, attempt_id=f"seed-{ref}", sample={
            "source_at": iso(source), "checked_at": iso(source), "fresh_until": iso(source + timedelta(seconds=60)),
            "allowed": True, "limit_reached": False,
            "primary": {"used_percent": 10, "remaining_percent": 90, "reset_at": None, "window_seconds": None},
            "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
        })
    ordered = QuotaRefreshCoordinator(accounts, quota_store=store)._priority(accounts.list(), store.read())
    assert [account.ref for account in ordered] == ["c", "a", "b"]


def test_clock_rollback_invalidates_live_store_sample_and_clock_disagreement_fails_check(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    wall = [datetime(2026, 9, 11, tzinfo=timezone.utc)]
    mono = [0.0]
    store = QuotaStore(root, now=lambda: wall[0], monotonic=lambda: mono[0])
    store.claim([account], attempt_id="clock-seed", owner_id="seed", deadline_at=wall[0] + timedelta(seconds=20))
    assert store.commit_success(account, attempt_id="clock-seed", sample={
        "source_at": iso(wall[0]), "checked_at": iso(wall[0]), "fresh_until": iso(wall[0] + timedelta(seconds=60)),
        "allowed": True, "limit_reached": False,
        "primary": {"used_percent": 10, "remaining_percent": 90, "reset_at": None, "window_seconds": None},
        "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
    })
    assert store.view_for(account, store.read())["current"] is not None
    wall[0] += timedelta(seconds=10)
    mono[0] += 10
    assert store.view_for(account, store.read())["current"] is not None
    wall[0] -= timedelta(seconds=15)
    mono[0] += 1
    rolled = store.view_for(account, store.read())
    assert rolled["current"] is None
    assert rolled["exclusion_reason"] == "clock_changed"

    disagreement_root = tmp_path / "disagreement"
    disagreement_root.mkdir()
    disagreement_auth = disagreement_root / "auth.json"
    _auth(disagreement_auth)
    disagreement_accounts = AccountStore(disagreement_root / "pool.json")
    disagreement_accounts.import_account("a", str(disagreement_auth))
    disagreement_wall = [datetime(2026, 9, 11, tzinfo=timezone.utc)]

    def disagreeing_reader(path: str, **kwargs) -> QuotaResult:
        disagreement_wall[0] += timedelta(seconds=3)
        return QuotaResult(10.0, None, None, None, iso(disagreement_wall[0]))

    disagreement_store = QuotaStore(disagreement_root, now=lambda: disagreement_wall[0], monotonic=lambda: 0.0)
    outcome = QuotaRefreshCoordinator(
        disagreement_accounts,
        quota_store=disagreement_store,
        reader=disagreeing_reader,
        clock=lambda: disagreement_wall[0],
        monotonic=lambda: 0.0,
    ).refresh()
    assert outcome.failed == 1
    assert disagreement_store.read()["accounts"]["a"]["error"]["code"] == "clock_changed"


def test_clock_generation_does_not_let_pre_rollback_check_clear_invalidation(tmp_path):
    root = tmp_path / "generation-clock"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    wall = [datetime(2026, 9, 11, tzinfo=timezone.utc)]
    mono = [0.0]
    store = QuotaStore(root, now=lambda: wall[0], monotonic=lambda: mono[0])
    sample = {
        "source_at": iso(wall[0]), "checked_at": iso(wall[0]),
        "fresh_until": iso(wall[0] + timedelta(seconds=60)),
        "allowed": True, "limit_reached": False,
        "primary": {"used_percent": 10, "remaining_percent": 90, "reset_at": None, "window_seconds": None},
        "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
    }
    store.claim([account], attempt_id="seed", owner_id="seed", deadline_at=wall[0] + timedelta(seconds=20))
    assert store.commit_success(account, attempt_id="seed", sample=sample)
    old_generation = store.begin_check(wall[0])
    wall[0] -= timedelta(seconds=15)
    mono[0] += 1
    assert store.view_for(account, store.read())["exclusion_reason"] == "clock_changed"

    store.claim([account], attempt_id="old-check", owner_id="seed", deadline_at=wall[0] + timedelta(seconds=20))
    old_sample = {**sample, "source_at": iso(wall[0]), "checked_at": iso(wall[0]), "fresh_until": iso(wall[0] + timedelta(seconds=60))}
    assert store.commit_success(account, attempt_id="old-check", sample=old_sample, clock_generation=old_generation)
    assert store.view_for(account, store.read())["exclusion_reason"] == "clock_changed"

    new_generation = store.begin_check(wall[0])
    store.claim([account], attempt_id="new-check", owner_id="seed", deadline_at=wall[0] + timedelta(seconds=20))
    assert store.commit_success(account, attempt_id="new-check", sample=old_sample, clock_generation=new_generation)
    assert store.view_for(account, store.read())["current"] is not None


@pytest.mark.asyncio
async def test_cancelled_owner_stops_work_and_cannot_commit_late_success(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    entered = threading.Event()
    calls = [0]

    def blocking_reader(path: str, *, cancel_event=None, **kwargs) -> QuotaResult:
        calls[0] += 1
        entered.set()
        while cancel_event is None or not cancel_event.is_set():
            time.sleep(0.005)
        # The coordinator must discard this result after observing cancellation.
        return QuotaResult(1.0, None, None, None, datetime.now(timezone.utc).isoformat())

    coordinator = QuotaRefreshCoordinator(accounts, reader=blocking_reader)
    task = asyncio.create_task(refresh_async(coordinator, timeout_seconds=10))
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = QuotaStore(root).read()["accounts"][account.ref]
    assert calls == [1]
    assert record["status"] == "failed"
    assert record["refresh"] is None
    assert record["error"]["code"] == "refresh_interrupted"


def test_task_local_timing_survives_cooldown_and_six_account_wave(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    for ref in ("a", "b", "c", "d", "e", "f"):
        accounts.import_account(ref, str(auth))
    wall = [datetime(2026, 9, 11, tzinfo=timezone.utc)]
    mono = [0.0]
    store = QuotaStore(root, now=lambda: wall[0], monotonic=lambda: mono[0])
    seeded = accounts.list()
    store.claim(seeded, attempt_id="old", owner_id="seed", deadline_at=wall[0] + timedelta(seconds=20))
    for account in seeded:
        assert store.commit_failure(
            account,
            attempt_id="old",
            failure={"code": "quota_http_error", "message": "seed", "http_status": None, "retryable": True},
        )

    active = [0]
    maximum = [0]
    active_lock = threading.Lock()

    def sleep(seconds: float) -> None:
        wall[0] += timedelta(seconds=seconds)
        mono[0] += seconds

    def reader(path: str, **kwargs) -> QuotaResult:
        with active_lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
        try:
            return QuotaResult(10.0, None, None, None, iso(wall[0]))
        finally:
            with active_lock:
                active[0] -= 1

    outcome = QuotaRefreshCoordinator(
        accounts,
        quota_store=store,
        reader=reader,
        clock=lambda: wall[0],
        monotonic=lambda: mono[0],
        sleep=sleep,
    ).refresh(timeout_seconds=22)

    assert outcome.status == "completed"
    assert outcome.succeeded == 6
    assert outcome.failed == 0
    assert maximum[0] <= 2
    assert mono[0] >= 5.0
    assert all(record.get("status") == "ok" for record in store.read()["accounts"].values())


def test_owner_budget_is_twenty_seconds_and_only_late_tasks_deadline(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    for ref in ("a", "b", "c", "d", "e", "f"):
        accounts.import_account(ref, str(auth))
    wall = [datetime(2026, 9, 11, tzinfo=timezone.utc)]
    mono = [0.0]
    state_lock = threading.Lock()

    def reader(path: str, *, deadline: float | None = None, **kwargs) -> QuotaResult:
        with state_lock:
            if deadline is not None and mono[0] >= deadline:
                raise TimeoutError("owner deadline")
            wall[0] += timedelta(seconds=4)
            mono[0] += 4.0
            if deadline is not None and mono[0] > deadline:
                raise TimeoutError("owner deadline")
            observed = iso(wall[0])
        return QuotaResult(10.0, None, None, None, observed)

    store = QuotaStore(root, now=lambda: wall[0], monotonic=lambda: mono[0])
    outcome = QuotaRefreshCoordinator(
        accounts,
        quota_store=store,
        reader=reader,
        clock=lambda: wall[0],
        monotonic=lambda: mono[0],
    ).refresh(timeout_seconds=22)

    assert mono[0] <= 20.0
    assert outcome.failed >= 1
    assert outcome.succeeded + outcome.failed == 6
    records = store.read()["accounts"]
    assert any((record.get("error") or {}).get("code") == "refresh_deadline" for record in records.values())
    assert not any((record.get("error") or {}).get("code") == "clock_changed" for record in records.values())


@pytest.mark.asyncio
async def test_owner_cancellation_interrupts_held_auth_lock_and_releases_refresh_lock(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    auth_data = json.loads(auth.read_text(encoding="utf-8"))
    auth_data["expires_at"] = 0
    auth.write_text(json.dumps(auth_data), encoding="utf-8")
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"access_token": "never-used", "expires_in": 3600})

    coordinator = QuotaRefreshCoordinator(
        accounts,
        transport=httpx.MockTransport(handler),
    )
    auth_lock = FileLock(str(auth.with_suffix(".json.lock")))
    auth_lock.acquire()
    try:
        task = asyncio.create_task(refresh_async(coordinator, timeout_seconds=10))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 2.0
    finally:
        auth_lock.release()

    record = QuotaStore(root).read()["accounts"][account.ref]
    assert calls == []
    assert record["status"] == "failed"
    assert record["error"]["code"] == "refresh_interrupted"
    refresh_lock = FileLock(str(root / "quota-v1.refresh.lock"))
    refresh_lock.acquire(timeout=0.2)
    refresh_lock.release()


@pytest.mark.asyncio
async def test_owner_cancellation_interrupts_slow_token_body_without_wham(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    auth_data = json.loads(auth.read_text(encoding="utf-8"))
    auth_data["expires_at"] = 0
    auth.write_text(json.dumps(auth_data), encoding="utf-8")
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    token_started = threading.Event()
    closed: list[bool] = []
    calls: list[str] = []

    class SlowTokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            token_started.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"{"

        async def aclose(self) -> None:
            closed.append(True)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, stream=SlowTokenStream())

    coordinator = QuotaRefreshCoordinator(accounts, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(refresh_async(coordinator, timeout_seconds=10))
    assert await asyncio.to_thread(token_started.wait, 1.0)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 2.0
    record = QuotaStore(root).read()["accounts"][account.ref]
    assert calls == ["POST"]
    assert closed == [True]
    assert record["status"] == "failed"
    assert record["error"]["code"] == "refresh_interrupted"
    refresh_lock = FileLock(str(root / "quota-v1.refresh.lock"))
    refresh_lock.acquire(timeout=0.2)
    refresh_lock.release()


@pytest.mark.asyncio
async def test_owner_cancellation_aborts_blocked_token_body_without_hidden_worker(tmp_path):
    root = tmp_path / "blocked-owner"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    auth_data = json.loads(auth.read_text(encoding="utf-8"))
    auth_data["expires_at"] = 0
    auth.write_text(json.dumps(auth_data), encoding="utf-8")
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    started = threading.Event()
    closed: list[bool] = []
    calls: list[str] = []

    class BlockedTokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(0.02)
            yield b"{"
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.append(True)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, stream=BlockedTokenStream())

    coordinator = QuotaRefreshCoordinator(accounts, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(refresh_async(coordinator, timeout_seconds=10))
    assert await asyncio.to_thread(started.wait, 1.0)
    started_cancel = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started_cancel < 2.0
    await asyncio.sleep(0.05)

    record = QuotaStore(root).read()["accounts"][account.ref]
    assert calls == ["POST"]
    assert closed == [True]
    assert record["status"] == "failed"
    assert record["refresh"] is None
    assert record["error"]["code"] == "refresh_interrupted"
    refresh_lock = FileLock(str(root / "quota-v1.refresh.lock"))
    refresh_lock.acquire(timeout=0.2)
    refresh_lock.release()


def test_malformed_sidecar_is_preserved_and_mutation_fences_epoch(tmp_path):
    auth = tmp_path / "auth.json"
    _auth(auth)
    accounts = AccountStore(tmp_path / "pool.json")
    account = accounts.import_account("a", str(auth))
    sidecar = tmp_path / "quota-v1.json"
    sidecar.write_text("{broken", encoding="utf-8")
    with pytest.raises(QuotaStateError):
        QuotaStore(tmp_path).read()
    assert sidecar.read_text(encoding="utf-8") == "{broken"
    # A config mutation must not make the malformed sidecar look valid or
    # revive an old sample.
    with pytest.raises(QuotaStateError):
        accounts.set_enabled("a", False)
    assert accounts.get("a").quota_epoch != account.quota_epoch
    assert sidecar.read_text(encoding="utf-8") == "{broken"


def test_sidecar_atomic_write_ignores_orphan_temp_and_keeps_private_mode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    account = accounts.import_account("a", str(auth))
    now = datetime.now(timezone.utc)
    sidecar = QuotaStore(root)
    sidecar.claim([account], attempt_id="atomic", owner_id="test", deadline_at=now + timedelta(seconds=20))
    assert sidecar.commit_success(account, attempt_id="atomic", sample={
        "source_at": iso(now), "checked_at": iso(now), "fresh_until": iso(now + timedelta(seconds=60)),
        "allowed": True, "limit_reached": False,
        "primary": {"used_percent": 5, "remaining_percent": 95, "reset_at": None, "window_seconds": None},
        "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
    })
    orphan = root / ".quota-v1.json.tmp.orphan"
    orphan.write_text("not-json", encoding="utf-8")
    assert sidecar.read()["accounts"]["a"]["status"] == "ok"
    assert (sidecar.quota_path.stat().st_mode & 0o777) == 0o600
    assert list(root.glob(".quota-v1.json.tmp.*")) == [orphan]


def test_agent_scripts_have_one_json_object_and_distinct_exit_behavior(tmp_path):
    env = dict(os.environ, CODEX_POOL_HOME=str(tmp_path))
    root = [sys.executable, "-m", "subs_pool.agent_cli"]
    machine = subprocess.run([*root, "--version"], env=env, text=True, capture_output=True, check=False)
    assert machine.returncode == 0
    assert len(machine.stdout.splitlines()) == 1
    assert json.loads(machine.stdout)["command"] == "version"
    quota = subprocess.run([*root, "codex", "quota"], env=env, text=True, capture_output=True, check=False)
    assert quota.returncode == 3
    assert len(quota.stdout.splitlines()) == 1
    payload = json.loads(quota.stdout)
    assert payload["ok"] is False
    assert payload["error"]["message"] == "no_checkable_accounts"
    assert quota.stderr == ""


def test_agent_help_and_trailing_options_are_strict_and_state_free(tmp_path):
    env = dict(os.environ, CODEX_POOL_HOME=str(tmp_path))
    root = [sys.executable, "-m", "subs_pool.agent_cli"]
    help_result = subprocess.run([*root, "codex", "status", "--help"], env=env, text=True, capture_output=True, check=False)
    assert help_result.returncode == 0
    assert json.loads(help_result.stdout)["command"] == "codex.status"
    assert not (tmp_path / "pool.json").exists()
    bad = subprocess.run([*root, "codex", "account", "list", "trailing"], env=env, text=True, capture_output=True, check=False)
    assert bad.returncode == 2
    assert bad.stderr == ""
    assert not (tmp_path / "pool.json").exists()


def _multiprocess_reader(path: str, **kwargs) -> QuotaResult:
    auth_path = Path(path)
    counter_path = auth_path.parent / "reader-count"
    with FileLock(str(counter_path.with_suffix(".lock"))):
        count = int(counter_path.read_text() or "0") if counter_path.exists() else 0
        counter_path.write_text(str(count + 1))
    time.sleep(0.2)
    return QuotaResult(20.0, None, None, None, datetime.now(timezone.utc).isoformat())


def _run_refresh_process(root_text: str) -> None:
    root = Path(root_text)
    accounts = AccountStore(root / "pool.json")
    QuotaRefreshCoordinator(accounts, reader=_multiprocess_reader).refresh()


def test_two_processes_coalesce_one_refresh_wave(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    AccountStore(root / "pool.json").import_account("a", str(auth))
    context = multiprocessing.get_context("fork")
    first = context.Process(target=_run_refresh_process, args=(str(root),))
    second = context.Process(target=_run_refresh_process, args=(str(root),))
    first.start()
    time.sleep(0.05)
    second.start()
    first.join(10)
    second.join(10)
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert (root / "reader-count").read_text() == "1"


@pytest.mark.asyncio
async def test_proxy_fails_before_upstream_when_no_current_quota(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    auth = root / "auth.json"
    _auth(auth)
    accounts = AccountStore(root / "pool.json")
    accounts.import_account("a", str(auth))

    class NoCallUpstream:
        calls = 0

        async def stream(self, **kwargs):
            self.calls += 1
            yield {"type": "response.completed", "response": {"status": "completed", "output": []}}

    upstream = NoCallUpstream()
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key="local")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await client.post("/v1/responses", headers={"Authorization": "Bearer local"}, json={"model": "m", "input": []})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "quota_unavailable"
    assert upstream.calls == 0
