from __future__ import annotations

import asyncio

import pytest
from textual.widgets import DataTable, Static

from subs_pool.cli_client import CLIError
from subs_pool.modules.codex import tui_adapter
from subs_pool.modules.codex.tui import SubscriptionPoolApp


def _account(ref: str, *, enabled: bool = True, weight: int = 1) -> dict:
    return {"ref": ref, "enabled": enabled, "weight": weight, "auth_present": True, "authenticated": True}


def _quota_row(ref: str, *, used: float = 10.0, eligible: bool = True) -> dict:
    now = "2099-09-11T07:21:07+00:00"
    current = {
        "source_at": now,
        "checked_at": now,
        "primary": {"used_percent": used, "remaining_percent": 100 - used, "reset_at": "2099-09-11T09:00:00+00:00", "window_seconds": 3600},
        "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
    }
    return {"ref": ref, "freshness": "fresh", "eligible": eligible, "exclusion_reason": None if eligible else "exhausted", "current": current, "last_success": current, "attempted_at": now, "error": None}


class FakeCLIClient:
    def __init__(self, *, quota_rows: list[dict] | None = None) -> None:
        self.status_data = {"accounts": [_account("a"), _account("b", enabled=False)], "eligible_count": 1}
        self.quota_data = {"accounts": quota_rows or [_quota_row("a"), _quota_row("b", used=100, eligible=False)]}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.quota_started = asyncio.Event()
        self.quota_gate: asyncio.Event | None = None
        self.quota_error: CLIError | None = None

    async def status(self) -> dict:
        self.calls.append(("status", (), {}))
        return self.status_data

    async def quota(self) -> dict:
        self.calls.append(("quota", (), {}))
        self.quota_started.set()
        if self.quota_gate is not None:
            await self.quota_gate.wait()
        if self.quota_error:
            raise self.quota_error
        return self.quota_data

    async def accounts_import(self, ref: str, path: str, *, weight: int = 1) -> dict:
        self.calls.append(("accounts_import", (ref, path), {"weight": weight}))
        return _account(ref, weight=weight)

    async def pool_enable(self, ref: str) -> dict:
        self.calls.append(("pool_enable", (ref,), {}))
        return {"ref": ref}

    async def pool_disable(self, ref: str) -> dict:
        self.calls.append(("pool_disable", (ref,), {}))
        return {"ref": ref}

    async def pool_weight(self, ref: str, weight: int) -> dict:
        self.calls.append(("pool_weight", (ref, weight), {}))
        return {"ref": ref, "weight": weight}


async def test_startup_enters_checking_then_renders_only_shared_current_values():
    client = FakeCLIClient()
    client.quota_gate = asyncio.Event()
    app = SubscriptionPoolApp(client=client)
    async with app.run_test(size=(100, 24)) as pilot:
        await client.quota_started.wait()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert "CHECKING" in str(table.get_cell("a", "check"))
        assert "N/A" in str(table.get_cell("a", "primary"))
        client.quota_gate.set()
        await pilot.pause()
        assert "OK" in str(table.get_cell("a", "check"))
        assert "90%" in str(table.get_cell("a", "primary"))
        assert "N/A" in str(table.get_cell("a", "secondary"))


async def test_shutdown_during_quota_check_does_not_render_unmounted_widgets():
    client = FakeCLIClient()
    client.quota_gate = asyncio.Event()
    app = SubscriptionPoolApp(client=client)
    async with app.run_test(size=(100, 24)):
        await client.quota_started.wait()


async def test_u_forces_quota_and_r_reloads_metadata_then_quota():
    client = FakeCLIClient(quota_rows=[_quota_row("a")])
    app = SubscriptionPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await client.quota_started.wait()
        await pilot.pause()
        initial_quota = sum(name == "quota" for name, _, _ in client.calls)
        await pilot.press("u")
        await pilot.pause()
        assert sum(name == "quota" for name, _, _ in client.calls) == initial_quota + 1
        await pilot.press("r")
        await pilot.pause()
        assert sum(name == "status" for name, _, _ in client.calls) >= 2
        assert sum(name == "quota" for name, _, _ in client.calls) >= initial_quota + 2


async def test_periodic_tick_requests_target_refresh_off_render_path():
    client = FakeCLIClient(quota_rows=[_quota_row("a")])
    app = SubscriptionPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await client.quota_started.wait()
        await pilot.pause()
        before = sum(name == "quota" for name, _, _ in client.calls)
        app._last_quota_refresh = asyncio.get_running_loop().time() - 31
        app._quota_tick()
        await pilot.pause()
        assert sum(name == "quota" for name, _, _ in client.calls) == before + 1


async def test_metadata_actions_remain_frontend_delegations():
    client = FakeCLIClient(quota_rows=[])
    client.status_data = {"accounts": [_account("a")], "eligible_count": 1}
    app = SubscriptionPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert ("pool_disable", ("a",), {}) in client.calls


async def test_quota_error_is_visible_without_reusing_old_values():
    client = FakeCLIClient(quota_rows=[_quota_row("a")])
    client.quota_error = CLIError("quota unavailable")
    app = SubscriptionPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await client.quota_started.wait()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert "N/A" in str(table.get_cell("a", "primary"))
        assert "UNAVAILABLE" in str(table.get_cell("a", "check"))
        assert "quota unavailable" in str(app.query_one("#status-line", Static).content)


@pytest.mark.asyncio
async def test_codex_adapter_preserves_partial_data_when_refresh_error_is_null(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_POOL_HOME", str(tmp_path))
    partial = {"refresh": {"error": None}, "accounts": [{"ref": "a", "current": None}]}
    monkeypatch.setattr(tui_adapter, "quota_data", lambda *args, **kwargs: (partial, 3))
    adapter = tui_adapter.CodexTUIAdapter()

    with pytest.raises(CLIError) as raised:
        await adapter.quota()
    assert raised.value.data is partial
    assert raised.value.message == "quota unavailable"


@pytest.mark.asyncio
async def test_partial_quota_wave_keeps_success_and_marks_unsatisfied_row_reason():
    success = _quota_row("a", used=10)
    unsatisfied = {
        "ref": "b",
        "freshness": "failed",
        "eligible": False,
        "exclusion_reason": "refresh_unavailable",
        "current": None,
        "last_success": _quota_row("b", used=20)["current"],
        "attempted_at": "2099-09-11T07:21:07+00:00",
        "error": None,
    }
    client = FakeCLIClient(quota_rows=[])
    client.status_data = {"accounts": [_account("a"), _account("b")], "eligible_count": 1}
    client.quota_data = {
        "accounts": [success, unsatisfied],
        "refresh": {"status": "partial", "error": {"message": "one account unavailable"}},
    }
    client.quota_error = CLIError("one account unavailable", returncode=3, data=client.quota_data)
    app = SubscriptionPoolApp(client=client)
    async with app.run_test(size=(100, 24)) as pilot:
        await client.quota_started.wait()
        await pilot.pause()
        assert app._quota_by_ref["a"].state == "ok"
        assert app._quota_by_ref["a"].snapshot["primary_used_percent"] == 10.0
        assert app._quota_by_ref["b"].state == "unavailable"
        assert app._quota_by_ref["b"].error == "refresh_unavailable"


@pytest.mark.asyncio
async def test_fresh_exhausted_rows_keep_current_meters_on_startup_and_shared_inspection():
    exhausted = [_quota_row("a", used=100, eligible=False), _quota_row("b", used=100, eligible=False)]
    client = FakeCLIClient(quota_rows=exhausted)
    client.status_data = {
        "accounts": [
            {**_account("a"), **exhausted[0]},
            {**_account("b"), **exhausted[1]},
        ],
        "eligible_count": 0,
    }
    app = SubscriptionPoolApp(client=client)
    async with app.run_test(size=(100, 24)) as pilot:
        await client.quota_started.wait()
        await pilot.pause()
        table = app.query_one(DataTable)
        for ref in ("a", "b"):
            assert "EXHAUSTED" in str(table.get_cell(ref, "check"))
            assert "0%" in str(table.get_cell(ref, "primary"))

        # This is the path used by the one-second shared-state observer.
        app._inspect_shared_state()
        await pilot.pause()
        for ref in ("a", "b"):
            assert "EXHAUSTED" in str(table.get_cell(ref, "check"))
            assert "0%" in str(table.get_cell(ref, "primary"))
