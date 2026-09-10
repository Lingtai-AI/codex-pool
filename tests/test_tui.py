from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, Static

from codex_pool.cli_client import CLIError
from codex_pool.tui import CodexPoolApp


class _FakeLoginStream:
    """Fakes cli_client.LoginStream without subprocess/provider I/O."""

    def __init__(self, events: list[dict], *, block: bool = False) -> None:
        self._events = list(events)
        self._block = block
        self.cancelled = False
        self._unblock = asyncio.Event()

    def __aiter__(self) -> "_FakeLoginStream":
        return self

    async def __anext__(self) -> dict:
        if self._events:
            return self._events.pop(0)
        if self._block and not self.cancelled:
            await self._unblock.wait()
        raise StopAsyncIteration

    async def cancel(self) -> None:
        self.cancelled = True
        self._unblock.set()


class FakeCLIClient:
    """Fakes CLIClient: no subprocess or provider I/O, records calls."""

    def __init__(self, status: dict | None = None, quota: dict | None = None) -> None:
        self._status = status or {"accounts": [], "eligible_count": 0}
        self._quota = quota or {"accounts": []}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.status_error: CLIError | None = None
        self._login_events: dict[str, list[dict]] = {}
        self.login_block = False
        self.last_login_stream: _FakeLoginStream | None = None

    def set_login_events(self, ref: str, events: list[dict]) -> None:
        self._login_events[ref] = events

    async def status(self) -> dict:
        self.calls.append(("status", (), {}))
        if self.status_error is not None:
            raise self.status_error
        return self._status

    async def quota(self) -> dict:
        self.calls.append(("quota", (), {}))
        return self._quota

    async def accounts_import(self, ref: str, path: str, *, weight: int = 1) -> dict:
        self.calls.append(("accounts_import", (ref, path), {"weight": weight}))
        return {"ref": ref, "enabled": True, "weight": weight, "auth_present": True, "quota": "unknown"}

    async def pool_enable(self, ref: str) -> dict:
        self.calls.append(("pool_enable", (ref,), {}))
        return {"ref": ref}

    async def pool_disable(self, ref: str) -> dict:
        self.calls.append(("pool_disable", (ref,), {}))
        return {"ref": ref}

    async def pool_weight(self, ref: str, weight: int) -> dict:
        self.calls.append(("pool_weight", (ref, weight), {}))
        return {"ref": ref, "weight": weight}

    def login(self, ref: str) -> _FakeLoginStream:
        self.calls.append(("login", (ref,), {}))
        stream = _FakeLoginStream(self._login_events.get(ref, []), block=self.login_block)
        self.last_login_stream = stream
        return stream


def _account(ref: str, *, enabled: bool = True, weight: int = 1, auth_present: bool = True) -> dict:
    return {"ref": ref, "enabled": enabled, "weight": weight, "auth_present": auth_present, "quota": "unknown"}


async def test_initial_load_renders_accounts_and_status():
    client = FakeCLIClient(
        status={"accounts": [_account("a"), _account("b", enabled=False, weight=2)], "eligible_count": 1}
    )
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 2
        status = app.query_one("#status-line", Static)
        assert "2 account(s), 1 eligible" in str(status.content)


async def test_status_error_shows_error_state():
    client = FakeCLIClient()
    client.status_error = CLIError("backend exploded")
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one("#status-line", Static)
        assert "backend exploded" in str(status.content)
        assert status.has_class("error")


async def test_toggle_enabled_calls_pool_disable_for_enabled_account():
    client = FakeCLIClient(status={"accounts": [_account("a", enabled=True)], "eligible_count": 1})
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert ("pool_disable", ("a",), {}) in client.calls


async def test_toggle_enabled_calls_pool_enable_for_disabled_account():
    client = FakeCLIClient(status={"accounts": [_account("a", enabled=False)], "eligible_count": 0})
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert ("pool_enable", ("a",), {}) in client.calls


async def test_weight_up_increments_and_weight_down_clamps_at_one():
    client = FakeCLIClient(status={"accounts": [_account("a", weight=1)], "eligible_count": 1})
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("plus")
        await pilot.pause()
        assert ("pool_weight", ("a", 2), {}) in client.calls
        await pilot.press("minus")
        await pilot.pause()
        assert ("pool_weight", ("a", 1), {}) in client.calls


async def test_set_weight_modal_sends_typed_value():
    client = FakeCLIClient(status={"accounts": [_account("a", weight=1)], "eligible_count": 1})
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        weight_input = app.screen.query_one("#weight-input", Input)
        weight_input.value = "9"
        await pilot.click("#weight-confirm")
        await pilot.pause()
        assert ("pool_weight", ("a", 9), {}) in client.calls


async def test_import_modal_calls_accounts_import_with_explicit_fields():
    client = FakeCLIClient()
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one("#import-ref", Input).value = "newacct"
        app.screen.query_one("#import-path", Input).value = "/tmp/auth.json"
        app.screen.query_one("#import-weight", Input).value = "4"
        await pilot.click("#import-confirm")
        await pilot.pause()
        assert ("accounts_import", ("newacct", "/tmp/auth.json"), {"weight": 4}) in client.calls


async def test_import_modal_cancel_does_not_call_import():
    client = FakeCLIClient()
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        await pilot.click("#import-cancel")
        await pilot.pause()
        assert not any(name == "accounts_import" for name, _, _ in client.calls)


async def test_quota_refresh_displays_returned_facts():
    client = FakeCLIClient(
        quota={"accounts": [{"ref": "a", "quota": {"primary_used_percent": 30, "secondary_used_percent": 10}}]}
    )
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        status = app.query_one("#status-line", Static)
        assert "primary=30%" in str(status.content)
        assert "secondary=10%" in str(status.content)


async def test_login_flow_shows_device_code_then_completes_and_refreshes():
    client = FakeCLIClient(status={"accounts": [_account("a", auth_present=False)], "eligible_count": 0})
    client.set_login_events(
        "a",
        [
            {
                "event": "authorization_required",
                "verification_uri": "https://example.com/device",
                "user_code": "ABCD-EFGH",
                "expires_in": 900,
                "interval": 5,
            },
            {"event": "completed", "account": _account("a", auth_present=True)},
        ],
    )
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        app.screen.query_one("#login-ref", Input).value = "a"
        await pilot.click("#login-start")
        await pilot.pause()
        login_status = app.screen.query_one("#login-status", Static)
        assert "Login completed." in str(login_status.content)
        status_calls_before_close = sum(1 for name, _, _ in client.calls if name == "status")
        await pilot.click("#login-cancel")
        await pilot.pause()
        assert not app.query("#login-dialog")
        status_calls_after_close = sum(1 for name, _, _ in client.calls if name == "status")
        assert status_calls_after_close == status_calls_before_close + 1


async def test_login_cancel_mid_flight_calls_stream_cancel_and_closes_dialog():
    client = FakeCLIClient()
    client.set_login_events(
        "a",
        [{"event": "authorization_required", "verification_uri": "https://example.com/device", "user_code": "ABCD-EFGH", "expires_in": 900, "interval": 5}],
    )
    client.login_block = True
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        app.screen.query_one("#login-ref", Input).value = "a"
        await pilot.click("#login-start")
        await pilot.pause()
        login_status = app.screen.query_one("#login-status", Static)
        assert "ABCD-EFGH" in str(login_status.content)
        await pilot.click("#login-cancel")
        await pilot.pause()
        assert not app.query("#login-dialog")
        assert client.last_login_stream is not None
        assert client.last_login_stream.cancelled is True


async def test_missing_selection_shows_error_instead_of_crashing():
    client = FakeCLIClient(status={"accounts": [], "eligible_count": 0})
    app = CodexPoolApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        status = app.query_one("#status-line", Static)
        assert "select an account first" in str(status.content)
        assert status.has_class("error")
