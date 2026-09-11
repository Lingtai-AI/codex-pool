"""Bounded, coalescing Codex quota refresh coordinator."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout

from .accounts import Account, AccountError, AccountStore
from .quota import QuotaResult, read_quota
from .quota_store import (
    MAX_AGE_SECONDS,
    QuotaStateError,
    QuotaStore,
    iso,
    parse_time,
    sanitized_error,
)

ACCOUNT_BUDGET_SECONDS = 8.0
OWNER_BUDGET_SECONDS = 20.0
CALL_BUDGET_SECONDS = 22.0
MIN_ATTEMPT_INTERVAL_SECONDS = 5.0
MAX_CONCURRENT_REQUESTS = 2


@dataclass
class _TaskObservation:
    result: QuotaResult | BaseException
    started_wall: datetime
    started_mono: float
    ended_wall: datetime
    ended_mono: float
    clock_generation: int


@dataclass
class RefreshOutcome:
    status: str
    checkable_count: int
    succeeded: int = 0
    failed: int = 0
    error: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    unsatisfied_refs: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.status in {"completed", "coalesced"}
            and self.failed == 0
            and not self.unsatisfied_refs
            and self.error is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checkable_count": self.checkable_count,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "error": self.error,
            "unsatisfied_refs": list(self.unsatisfied_refs),
        }


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), timezone.utc)


def _failure_for(result: QuotaResult | BaseException) -> dict[str, Any]:
    if isinstance(result, QuotaResult):
        raw = result.error or "quota check failed"
        if raw.startswith("http_status_"):
            try:
                status = int(raw.removeprefix("http_status_"))
            except ValueError:
                status = None
            return sanitized_error("quota_http_error", "quota endpoint returned an error", http_status=status)
        if raw in {"request_timeout", "timeout"}:
            return sanitized_error("quota_timeout", "quota check timed out")
        if raw.startswith("auth_"):
            return sanitized_error("auth_unavailable", "account authentication is unavailable")
        if raw.startswith("quota_fields") or raw.startswith("response_"):
            return sanitized_error("invalid_quota_response", "quota response was invalid")
        return sanitized_error("quota_http_error", "quota check was unavailable")
    if isinstance(result, TimeoutError):
        return sanitized_error("refresh_deadline", "quota check exceeded its deadline")
    return sanitized_error("quota_http_error", "quota check was unavailable")


def _sample_from_result(
    result: QuotaResult,
    *,
    started: datetime,
    checked: datetime,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    if elapsed_seconds is not None and (
        elapsed_seconds < 0 or elapsed_seconds > ACCOUNT_BUDGET_SECONDS
    ):
        raise TimeoutError("quota reader exceeded its deadline")
    if result.status != "ok" or result.primary_used_percent is None:
        raise ValueError("primary quota window is unavailable")
    if result.secondary_malformed:
        raise ValueError("secondary quota window is malformed")

    source = parse_time(result.observed_at)
    if source is None:
        raise ValueError("quota sample source timestamp is invalid")
    if source > checked:
        raise ValueError("quota sample timestamps are invalid")
    wall_duration = (checked - started).total_seconds()
    if wall_duration < 0 or (elapsed_seconds is not None and abs(wall_duration - elapsed_seconds) > 1.0):
        raise ValueError("quota check clock changed")

    reset_times = [parse_time(value) for value in (result.primary_reset_at, result.secondary_reset_at)]
    reset_times = [value for value in reset_times if value is not None]
    if any(value <= checked for value in reset_times):
        raise ValueError("quota sample reset boundary has passed")
    fresh_until = min([source + timedelta(seconds=MAX_AGE_SECONDS), *reset_times])

    def window(used: float | None, reset: str | None, duration_mins: float | None) -> dict[str, Any]:
        return {
            "used_percent": used,
            "remaining_percent": (100.0 - used) if used is not None else None,
            "reset_at": reset,
            "window_seconds": duration_mins * 60.0 if duration_mins is not None else None,
        }

    exhausted = result.limit_reached if isinstance(result.limit_reached, bool) else result.exhausted is True
    allowed = result.allowed if isinstance(result.allowed, bool) else True
    return {
        "source_at": iso(source),
        "checked_at": iso(checked),
        "fresh_until": iso(fresh_until),
        "allowed": allowed,
        "limit_reached": exhausted,
        "primary": window(result.primary_used_percent, result.primary_reset_at, result.primary_window_duration_mins),
        "secondary": window(result.secondary_used_percent, result.secondary_reset_at, result.secondary_window_duration_mins),
    }


class QuotaRefreshCoordinator:
    """One pool-wide refresh owner shared by CLI, TUI, and proxy."""

    def __init__(
        self,
        accounts: AccountStore,
        *,
        quota_store: QuotaStore | None = None,
        reader: Callable[..., QuotaResult] | None = None,
        transport: Any = None,
        clock: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.accounts = accounts
        self.store = quota_store or QuotaStore(accounts.root)
        self.reader = reader or read_quota
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.sleep = sleep
        self.owner_id = str(uuid.uuid4())

    def _now(self) -> datetime:
        return _as_datetime(self.clock())

    def _checkable(self, accounts: list[Account]) -> list[Account]:
        return [
            account
            for account in accounts
            if account.enabled and account.local_authenticated(root=self.accounts.root)
        ]

    @staticmethod
    def _priority(accounts: list[Account], snapshot: dict[str, Any]) -> list[Account]:
        records = snapshot.get("accounts", {})

        def key(account: Account) -> tuple[int, float, str]:
            record = records.get(account.ref, {}) if isinstance(records, dict) else {}
            sample = record.get("last_success") if isinstance(record, dict) else None
            source = parse_time(sample.get("source_at")) if isinstance(sample, dict) else None
            current_epoch = isinstance(record, dict) and record.get("quota_epoch") == account.quota_epoch
            never_checked = not current_epoch or source is None
            return (0 if never_checked else 1, source.timestamp() if source is not None else 0.0, account.ref)

        return sorted(accounts, key=key)

    def _read_result(
        self,
        account: Account,
        cancel_event: threading.Event | None = None,
        *,
        deadline: float | None = None,
    ) -> QuotaResult:
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("quota refresh cancelled")
        kwargs: dict[str, Any] = {"timeout_seconds": ACCOUNT_BUDGET_SECONDS}
        kwargs["cancel_event"] = cancel_event
        if deadline is not None:
            kwargs["deadline"] = deadline
            kwargs["monotonic_fn"] = self.monotonic
        if self.transport is not None:
            kwargs["transport"] = self.transport
        result = self.reader(str(account.resolved_auth_path(self.accounts.root)), **kwargs)
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("quota refresh cancelled")
        return result

    def _wait_for_owner(
        self,
        before_snapshot: dict[str, Any],
        deadline: float,
        checkable: list[Account],
        cancel_event: threading.Event | None = None,
    ) -> RefreshOutcome:
        before_revision = int(before_snapshot.get("revision", 0))
        while self.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return RefreshOutcome(
                    "timed_out", len(checkable),
                    error=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True),
                    unsatisfied_refs=tuple(account.ref for account in checkable),
                )
            try:
                accounts, snapshot = self.store.read_consistent(self.accounts)
            except (AccountError, QuotaStateError, OSError):
                return RefreshOutcome("unavailable", len(checkable), error=sanitized_error("state_unavailable", "quota state is unavailable", retryable=True))
            records = snapshot.get("accounts", {})
            completed = all(
                isinstance(records.get(account.ref), dict)
                and records[account.ref].get("status") in {"ok", "failed"}
                and (
                    records[account.ref].get("attempt_id") != before_snapshot.get("accounts", {}).get(account.ref, {}).get("attempt_id")
                    or before_snapshot.get("accounts", {}).get(account.ref, {}).get("status") == "checking"
                )
                for account in checkable
            )
            if snapshot.get("revision", 0) > before_revision and completed:
                rows = {row["id"]: row for row in self.store.rows(accounts, snapshot, now=self._now())}
                failed = sum(1 for account in checkable if rows.get(account.ref, {}).get("status") == "failed")
                return RefreshOutcome("coalesced", len(checkable), succeeded=len(checkable) - failed, failed=failed, snapshot=snapshot)
            self.sleep(min(0.05, max(0.001, deadline - self.monotonic())))
        return RefreshOutcome(
            "timed_out", len(checkable), error=sanitized_error("refresh_deadline", "another quota refresh did not finish"),
            unsatisfied_refs=tuple(account.ref for account in checkable),
        )

    def refresh(
        self,
        *,
        force: bool = True,
        timeout_seconds: float = CALL_BUDGET_SECONDS,
        cancel_event: threading.Event | None = None,
    ) -> RefreshOutcome:
        cancel_event = cancel_event or threading.Event()
        started_mono = self.monotonic()
        deadline_mono = started_mono + min(timeout_seconds, CALL_BUDGET_SECONDS)
        if cancel_event.is_set():
            return RefreshOutcome("timed_out", 0, error=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True))
        try:
            accounts, before = self.store.read_consistent(self.accounts)
        except (AccountError, QuotaStateError, OSError):
            return RefreshOutcome("unavailable", 0, error=sanitized_error("state_unavailable", "quota state is unavailable", retryable=True))
        checkable = self._priority(self._checkable(accounts), before)
        if not checkable:
            return RefreshOutcome("unavailable", 0, error=sanitized_error("auth_unavailable", "no_checkable_accounts", retryable=False))

        lock = FileLock(str(self.store.refresh_lock_path), timeout=0)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return self._wait_for_owner(before, deadline_mono, checkable, cancel_event)

        try:
            owner_started_mono = self.monotonic()
            owner_deadline_mono = owner_started_mono + OWNER_BUDGET_SECONDS
            effective_deadline_mono = min(deadline_mono, owner_deadline_mono)
            try:
                # A process that obtains this kernel lock is the new owner;
                # checking markers left by a crashed owner are therefore
                # safely repairable before this wave starts.
                self.store.repair_abandoned()
                accounts, current = self.store.read_consistent(self.accounts)
                checkable = self._priority(self._checkable(accounts), current)
                if not checkable:
                    return RefreshOutcome("unavailable", 0, error=sanitized_error("auth_unavailable", "no_checkable_accounts", retryable=False), snapshot=current)

                # Do not start a second request inside the five-second stampede
                # window. The caller receives a bounded conservative outcome.
                now = self._now()
                if not force:
                    due = False
                    due_accounts = []
                    for account in checkable:
                        record = current.get("accounts", {}).get(account.ref, {})
                        sample = record.get("last_success") if isinstance(record, dict) else None
                        source = parse_time(sample.get("source_at")) if isinstance(sample, dict) else None
                        attempted = parse_time(record.get("attempted_at")) if isinstance(record, dict) else None
                        source_age = (now - source).total_seconds() if source is not None else None
                        attempt_age = (now - attempted).total_seconds() if attempted is not None else None
                        if isinstance(record, dict) and record.get("status") == "failed" and attempt_age is not None and 0 <= attempt_age < 30.0:
                            continue
                        if source is None or source_age < 0 or source_age >= 30.0 or (attempt_age is not None and attempt_age >= 30.0):
                            if attempt_age is None or attempt_age >= MIN_ATTEMPT_INTERVAL_SECONDS:
                                due_accounts.append(account)
                    due = bool(due_accounts)
                    if not due:
                        return RefreshOutcome("coalesced", len(checkable), succeeded=len(checkable), snapshot=current)
                    selected = due_accounts
                else:
                    selected = list(checkable)
                if force:
                    selected = []
                    blocked_by_cooldown = []
                    max_wait = 0.0
                    for account in checkable:
                        record = current.get("accounts", {}).get(account.ref, {})
                        attempted = parse_time(record.get("attempted_at")) if isinstance(record, dict) else None
                        attempt_age = (now - attempted).total_seconds() if attempted is not None else None
                        if attempt_age is not None and attempt_age < MIN_ATTEMPT_INTERVAL_SECONDS:
                            blocked_by_cooldown.append(account)
                            # Future wall timestamps can result from a clock
                            # rollback. They must not create an unbounded
                            # wait; the monotonic stampede guard is capped at
                            # the same five-second interval.
                            max_wait = max(
                                max_wait,
                                min(MIN_ATTEMPT_INTERVAL_SECONDS, max(0.0, MIN_ATTEMPT_INTERVAL_SECONDS - attempt_age)),
                            )
                        else:
                            selected.append(account)
                    if blocked_by_cooldown and max_wait > 0:
                        if self.monotonic() + max_wait <= effective_deadline_mono:
                            remaining_wait = max_wait
                            while remaining_wait > 0 and not cancel_event.is_set():
                                wait_for = min(0.05, remaining_wait)
                                wait_started = self.monotonic()
                                if cancel_event.wait(0):
                                    break
                                self.sleep(wait_for)
                                remaining_wait -= max(0.001, self.monotonic() - wait_started)
                            if cancel_event.is_set():
                                return RefreshOutcome(
                                    "timed_out", len(checkable),
                                    error=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True),
                                    snapshot=current, unsatisfied_refs=tuple(a.ref for a in checkable),
                                )
                            # The persisted wall clock may be fake or slow in
                            # tests; monotonic waiting is the actual cooldown
                            # boundary for this owner.
                            selected = list(checkable)
                        else:
                            selected = [account for account in checkable if account not in blocked_by_cooldown]
                    if not selected:
                        return RefreshOutcome(
                            "unavailable", len(checkable), error=sanitized_error("refresh_deadline", "quota refresh cooldown is active", retryable=True),
                            snapshot=current, unsatisfied_refs=tuple(a.ref for a in checkable)
                        )

                if cancel_event.is_set():
                    return RefreshOutcome(
                        "timed_out", len(checkable),
                        error=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True),
                        snapshot=current, unsatisfied_refs=tuple(a.ref for a in checkable),
                    )

                if self.monotonic() >= effective_deadline_mono:
                    return RefreshOutcome(
                        "unavailable", len(checkable),
                        error=sanitized_error("refresh_deadline", "quota refresh owner deadline elapsed", retryable=True),
                        snapshot=current,
                        unsatisfied_refs=tuple(account.ref for account in checkable) if force else (),
                    )

                claim_now = self._now()
                attempt_id = str(uuid.uuid4())
                self.store.claim(
                    selected,
                    attempt_id=attempt_id,
                    owner_id=self.owner_id,
                    deadline_at=claim_now + timedelta(seconds=max(0.0, effective_deadline_mono - self.monotonic())),
                )

                results: dict[str, _TaskObservation | BaseException] = {}
                interrupted_marked: set[str] = set()
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS)
                futures: dict[concurrent.futures.Future, Account] = {}
                next_account = 0
                stop_reason: str | None = None

                def run_account(account: Account) -> _TaskObservation:
                    started_wall = self._now()
                    started_mono = self.monotonic()
                    clock_generation = self.store.begin_check(started_wall)
                    task_deadline = min(
                        started_mono + ACCOUNT_BUDGET_SECONDS,
                        effective_deadline_mono,
                    )
                    try:
                        raw: QuotaResult | BaseException = self._read_result(
                            account,
                            cancel_event,
                            deadline=task_deadline,
                        )
                    except BaseException as exc:  # provider boundary is fail-soft
                        raw = exc
                    ended_wall = self._now()
                    ended_mono = self.monotonic()
                    return _TaskObservation(raw, started_wall, started_mono, ended_wall, ended_mono, clock_generation)

                def schedule() -> None:
                    nonlocal next_account
                    while (
                        not cancel_event.is_set()
                        and self.monotonic() < effective_deadline_mono
                        and len(futures) < MAX_CONCURRENT_REQUESTS
                        and next_account < len(selected)
                    ):
                        account = selected[next_account]
                        next_account += 1
                        futures[executor.submit(run_account, account)] = account

                schedule()
                try:
                    while futures:
                        if cancel_event.is_set():
                            stop_reason = "cancelled"
                            break
                        remaining = min(ACCOUNT_BUDGET_SECONDS, effective_deadline_mono - self.monotonic())
                        if remaining <= 0:
                            stop_reason = "deadline"
                            cancel_event.set()
                            break
                        done, _ = concurrent.futures.wait(
                            tuple(futures), timeout=remaining,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        if not done:
                            stop_reason = "deadline"
                            cancel_event.set()
                            break
                        for future in done:
                            account = futures.pop(future)
                            try:
                                results[account.ref] = future.result()
                            except BaseException as exc:  # provider boundary is fail-soft
                                results[account.ref] = exc
                        schedule()
                    if next_account < len(selected) and not futures and stop_reason is None:
                        stop_reason = "deadline"
                        cancel_event.set()
                finally:
                    if cancel_event.is_set() and stop_reason is None:
                        stop_reason = "cancelled"
                    if stop_reason == "cancelled":
                        # Publish interruption before joining a cooperative
                        # worker. The refresh lock remains held until every
                        # worker has stopped, so this cannot race a late
                        # network result or commit.
                        for account in selected:
                            if account.ref not in results:
                                try:
                                    self.store.commit_failure(
                                        account,
                                        attempt_id=attempt_id,
                                        failure=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True),
                                    )
                                except (AccountError, QuotaStateError, OSError):
                                    pass
                                interrupted_marked.add(account.ref)
                    for future, account in list(futures.items()):
                        future.cancel()
                    executor.shutdown(wait=True, cancel_futures=True)
                    for future, account in list(futures.items()):
                        try:
                            results[account.ref] = future.result()
                        except BaseException as exc:
                            results.setdefault(account.ref, exc)

                if cancel_event.is_set() and stop_reason is None:
                    # Cancellation may arrive after the executor drained but
                    # before result classification. Treat that boundary as
                    # cancellation too; no late successful commit is allowed.
                    stop_reason = "cancelled"
                succeeded = failed = 0
                unsatisfied: set[str] = {a.ref for a in checkable if a not in selected}
                for account in selected:
                    observation = results.get(account.ref)
                    if stop_reason == "cancelled":
                        if account.ref not in interrupted_marked:
                            self.store.commit_failure(
                                account, attempt_id=attempt_id,
                                failure=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True),
                            )
                        failed += 1
                        unsatisfied.add(account.ref)
                        continue
                    if not isinstance(observation, _TaskObservation):
                        failure = (
                            sanitized_error("refresh_deadline", "quota check did not finish", retryable=True)
                            if stop_reason == "deadline"
                            else _failure_for(observation or RuntimeError())
                        )
                        self.store.commit_failure(account, attempt_id=attempt_id, failure=failure)
                        failed += 1
                        unsatisfied.add(account.ref)
                        continue
                    if observation.ended_mono >= effective_deadline_mono:
                        failure = sanitized_error("refresh_deadline", "quota check did not finish", retryable=True)
                        self.store.commit_failure(account, attempt_id=attempt_id, failure=failure)
                        failed += 1
                        unsatisfied.add(account.ref)
                        continue
                    raw = observation.result
                    if isinstance(raw, QuotaResult):
                        try:
                            sample = _sample_from_result(
                                raw,
                                started=observation.started_wall,
                                checked=observation.ended_wall,
                                elapsed_seconds=observation.ended_mono - observation.started_mono,
                            )
                        except (ValueError, TimeoutError) as exc:
                            code = "refresh_deadline" if isinstance(exc, TimeoutError) else ("clock_changed" if "clock" in str(exc) else ("sample_expired" if "reset" in str(exc) else "invalid_quota_response"))
                            failure = sanitized_error(code, "quota sample was not usable", retryable=code == "refresh_deadline")
                            self.store.commit_failure(account, attempt_id=attempt_id, failure=failure)
                            failed += 1
                            unsatisfied.add(account.ref)
                        else:
                            if cancel_event.is_set():
                                self.store.commit_failure(
                                    account,
                                    attempt_id=attempt_id,
                                    failure=sanitized_error("refresh_interrupted", "quota refresh was cancelled", retryable=True),
                                )
                                failed += 1
                                unsatisfied.add(account.ref)
                            elif self.store.commit_success(
                                account,
                                attempt_id=attempt_id,
                                sample=sample,
                                clock_generation=observation.clock_generation,
                            ):
                                succeeded += 1
                            else:
                                failed += 1
                                unsatisfied.add(account.ref)
                    else:
                        self.store.commit_failure(account, attempt_id=attempt_id, failure=_failure_for(raw or RuntimeError()))
                        failed += 1
                        unsatisfied.add(account.ref)

                try:
                    final_accounts, snapshot = self.store.read_consistent(self.accounts)
                except (AccountError, QuotaStateError, OSError):
                    return RefreshOutcome("unavailable", len(checkable), succeeded=succeeded, failed=failed, error=sanitized_error("state_unavailable", "quota state is unavailable", retryable=True))
                status = "completed" if failed == 0 and not unsatisfied else "partial"
                return RefreshOutcome(status, len(checkable), succeeded=succeeded, failed=failed, snapshot=snapshot, unsatisfied_refs=tuple(sorted(unsatisfied)) if force else ())
            except (AccountError, QuotaStateError, OSError) as exc:
                return RefreshOutcome("unavailable", len(checkable), error=sanitized_error("state_unavailable", "quota state is unavailable", retryable=True))
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass


async def refresh_async(coordinator: QuotaRefreshCoordinator, *, force: bool = True, timeout_seconds: float = CALL_BUDGET_SECONDS) -> RefreshOutcome:
    """Run the synchronous bounded coordinator off an async event loop."""
    cancel_event = threading.Event()
    work = asyncio.create_task(
        asyncio.to_thread(
            coordinator.refresh,
            force=force,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
    )
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Cancellation of the coroutine must not release the file lock while
        # the owner thread can still commit. Join the cooperative worker
        # before propagating cancellation.
        cancel_event.set()
        # The worker's provider boundary is cooperative and every built-in
        # blocking boundary is bounded by the same deadline/event. Await it
        # to completion before propagating cancellation; returning after a
        # timeout would leave an honest owner thread behind the caller.
        await asyncio.shield(work)
        raise


__all__ = ["QuotaRefreshCoordinator", "RefreshOutcome", "refresh_async"]
