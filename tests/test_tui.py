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
        self.quota_error: CLIError | None = None
        self.quota_gate: asyncio.Event | None = None
        self.quota_started = asyncio.Event()
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
        self.quota_started.set()
        if self.quota_gate is not None:
            await self.quota_gate.wait()
        if self.quota_error is not None:
            raise self.quota_error
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


async def test_quota_meter_selection_failure_and_metadata_preservation():
    client = FakeCLIClient(
        status={
            "accounts": [
                _account("synthetic-a"),
                _account("synthetic-b", weight=2),
            ],
            "eligible_count": 2,
        },
        quota={
            "accounts": [
                {
                    "ref": "synthetic-a",
                    "quota": {
                        "status": "ok",
                        "primary_used_percent": 30,
                        "secondary_used_percent": 10,
                        "primary_window_name": "reported burst",
                        "primary_window_duration_mins": 300,
                        "primary_reset_at": "2099-09-11T09:00:00+00:00",
                        "secondary_reset_at": None,
                        "observed_at": "2026-09-11T07:21:07+00:00",
                    },
                },
                {
                    "ref": "synthetic-b",
                    "quota": {
                        "status": "ok",
                        "primary_used_percent": 100,
                        "secondary_used_percent": None,
                        "primary_window_duration_mins": 60,
                        "primary_reset_at": "2099-09-11T10:00:00+00:00",
                        "observed_at": "2026-09-11T07:22:07+00:00",
                    },
                },
            ]
        },
    )
    app = CodexPoolApp(client=client)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        table = app.query_one(DataTable)
        assert "70%" in str(table.get_cell("synthetic-a", "primary"))
        assert "90%" in str(table.get_cell("synthetic-a", "secondary"))
        assert "0%" in str(table.get_cell("synthetic-b", "primary"))
        assert "N/A" in str(table.get_cell("synthetic-b", "secondary"))

        table.move_cursor(row=1)
        await pilot.pause()
        detail = app.query_one("#selected-detail", Static)
        assert "SELECTED synthetic-b" in str(detail.content)
        assert "0% remaining" in str(detail.content)
        assert "Duration 1h" in str(detail.content)
        assert "Reset 2099-09-11 10:00Z" in str(detail.content)
        assert "Observed 2026-09-11 07:22:07Z" in str(detail.content)
        assert "Last attempt N/A" not in str(detail.content)

        # A metadata-only refresh may reorder accounts and return quota=None,
        # but selection and the last quota observation remain keyed by ref.
        client._status = {
            "accounts": [
                {**_account("synthetic-b", weight=3), "quota": None},
                {**_account("synthetic-a"), "quota": None},
            ],
            "eligible_count": 2,
        }
        await pilot.press("r")
        await pilot.pause()
        assert app._selected_ref == "synthetic-b"
        assert "0%" in str(table.get_cell("synthetic-b", "primary"))
        assert "Weight 3" in str(detail.content)

        # A second all-account check clears old values before awaiting the CLI.
        client.quota_started = asyncio.Event()
        client.quota_gate = asyncio.Event()
        client.quota_error = CLIError("synthetic quota failure")
        await pilot.press("u")
        await client.quota_started.wait()
        await pilot.pause()
        assert "…" in str(table.get_cell("synthetic-a", "primary"))
        assert "…" in str(table.get_cell("synthetic-b", "primary"))

        calls_while_running = sum(1 for name, _, _ in client.calls if name == "quota")
        await pilot.press("u")
        await pilot.pause()
        assert sum(1 for name, _, _ in client.calls if name == "quota") == calls_while_running
        assert "already running" in str(app.query_one("#status-line", Static).content)

        client.quota_gate.set()
        await pilot.pause()
        assert "N/A" in str(table.get_cell("synthetic-a", "primary"))
        assert "N/A" in str(table.get_cell("synthetic-b", "primary"))
        assert "UNAVAILABLE" in str(detail.content)
        assert "synthetic quota failure" in str(detail.content)
        failed_attempt = app._quota_by_ref["synthetic-b"].attempted_at

        await pilot.press("r")
        await pilot.pause()
        assert app._selected_ref == "synthetic-b"
        assert app._quota_by_ref["synthetic-b"].attempted_at == failed_attempt
        assert "synthetic quota failure" in str(detail.content)
        assert "Quota check unavailable" in str(
            app.query_one("#status-line", Static).content
        )


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
