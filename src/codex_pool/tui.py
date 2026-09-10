"""Textual TUI — a pure frontend over the codex-pool CLI JSON contract.

No account/token storage, auth, or provider HTTP happens in this module:
every action runs a :class:`codex_pool.cli_client.CLIClient` command (a local
subprocess running ``python -m codex_pool ...``) and renders returned JSON
facts.
"""

from __future__ import annotations

import sys
from typing import Any

from .cli_client import CLIClient, CLIError, LoginStream


def _missing_textual_error() -> str:
    return (
        "the codex-pool TUI requires the 'textual' package "
        "(it is a mandatory dependency of codex-pool; reinstall codex-pool "
        "if this is missing)"
    )


try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static
except ImportError as _textual_import_error:  # pragma: no cover - exercised via run_tui()
    App = object  # type: ignore[assignment,misc]
    _TEXTUAL_IMPORT_ERROR: ImportError | None = _textual_import_error
else:
    _TEXTUAL_IMPORT_ERROR = None


if _TEXTUAL_IMPORT_ERROR is None:

    class ImportScreen(ModalScreen[tuple[str, str, int] | None]):
        """Modal collecting an explicit account ref, auth-file path, and weight."""

        BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

        def __init__(self) -> None:
            super().__init__()
            self._ref = Input(placeholder="account ref", id="import-ref")
            self._path = Input(placeholder="path to auth JSON file", id="import-path")
            self._weight = Input(placeholder="weight (default 1)", id="import-weight")

        def compose(self) -> ComposeResult:
            with Vertical(id="import-dialog"):
                yield Label("Import account (explicit path only)")
                yield self._ref
                yield self._path
                yield self._weight
                with Horizontal():
                    yield Button("Import", id="import-confirm", variant="primary")
                    yield Button("Cancel", id="import-cancel")

        def action_dismiss_none(self) -> None:
            self.dismiss(None)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "import-cancel":
                self.dismiss(None)
                return
            ref = self._ref.value.strip()
            path = self._path.value.strip()
            weight_raw = self._weight.value.strip() or "1"
            if not ref or not path:
                self.app.bell()
                return
            try:
                weight = int(weight_raw)
            except ValueError:
                self.app.bell()
                return
            self.dismiss((ref, path, weight))


    class WeightScreen(ModalScreen[int | None]):
        """Modal for setting an exact pool weight."""

        BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

        def __init__(self, ref: str, current: int) -> None:
            super().__init__()
            self._ref = ref
            self._input = Input(value=str(current), id="weight-input")

        def compose(self) -> ComposeResult:
            with Vertical(id="weight-dialog"):
                yield Label(f"Set weight: {self._ref}")
                yield self._input
                with Horizontal():
                    yield Button("Set", id="weight-confirm", variant="primary")
                    yield Button("Cancel", id="weight-cancel")

        def action_dismiss_none(self) -> None:
            self.dismiss(None)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "weight-cancel":
                self.dismiss(None)
                return
            try:
                weight = int(self._input.value.strip())
            except ValueError:
                self.app.bell()
                return
            self.dismiss(weight)


    class LoginScreen(ModalScreen[None]):
        """Modal collecting a ref and driving the device-login JSONL stream."""

        BINDINGS = [Binding("escape", "request_cancel", "Cancel")]

        def __init__(self, client: CLIClient, ref: str = "") -> None:
            super().__init__()
            self._client = client
            self._ref_input = Input(value=ref, placeholder="account ref", id="login-ref")
            self._status = Static("", id="login-status")
            self._start_button = Button("Start login", id="login-start", variant="primary")
            self._cancel_button = Button("Cancel", id="login-cancel")
            self._stream: LoginStream | None = None
            self._started = False

        def compose(self) -> ComposeResult:
            with Vertical(id="login-dialog"):
                yield Label("Device login")
                yield self._ref_input
                yield self._status
                with Horizontal():
                    yield self._start_button
                    yield self._cancel_button

        def action_request_cancel(self) -> None:
            self._cancel()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "login-cancel":
                self._cancel()
                return
            if event.button.id == "login-start" and not self._started:
                ref = self._ref_input.value.strip()
                if not ref:
                    self.app.bell()
                    return
                self._started = True
                self._ref_input.disabled = True
                self._start_button.disabled = True
                self._status.update("starting login…")
                self._run_login(ref)

        @work(exclusive=True)
        async def _run_login(self, ref: str) -> None:
            stream = self._client.login(ref)
            self._stream = stream
            try:
                async for event in stream:
                    self._render_event(event)
                    if event.get("event") == "completed":
                        break
            except CLIError as exc:
                self._safe_status(f"login failed: {exc.message}")

        def _render_event(self, event: dict[str, Any]) -> None:
            kind = event.get("event")
            if kind == "authorization_required":
                lines = [
                    "Open this URL to authorize:",
                    str(event.get("verification_uri", "")),
                    f"Code: {event.get('user_code', '')}",
                    f"Expires in {event.get('expires_in', '?')}s "
                    f"(polling every {event.get('interval', '?')}s)",
                ]
                self._safe_status("\n".join(lines))
            elif kind == "completed":
                self._safe_status("Login completed.")
                self._cancel_button.label = "Close"
            else:
                self._safe_status(f"event: {kind}")

        def _safe_status(self, text: str) -> None:
            try:
                self._status.update(text)
            except Exception:
                pass

        def _cancel(self) -> None:
            stream = self._stream
            self.dismiss(None)
            if stream is not None:
                self._background_cancel(stream)

        @work(exclusive=False)
        async def _background_cancel(self, stream: LoginStream) -> None:
            await stream.cancel()

        def on_unmount(self) -> None:
            if self._stream is not None:
                self._background_cancel(self._stream)


    class CodexPoolApp(App[None]):
        """Main accounts/status view with pool and auth actions."""

        CSS = """
        #import-dialog, #login-dialog, #weight-dialog {
            width: 64;
            height: auto;
            border: round $accent;
            background: $panel;
            padding: 1 2;
        }
        #import-dialog Input, #login-dialog Input, #weight-dialog Input {
            margin-bottom: 1;
        }
        #login-status {
            margin: 1 0;
            height: auto;
        }
        #status-line.error {
            color: $error;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("i", "import_account", "Import"),
            Binding("l", "login_account", "Login"),
            Binding("e", "toggle_enabled", "Enable/Disable"),
            Binding("plus", "weight_up", "Weight +"),
            Binding("minus", "weight_down", "Weight -"),
            Binding("w", "set_weight", "Set weight"),
            Binding("u", "refresh_quota", "Quota"),
            Binding("r", "refresh", "Refresh"),
        ]

        def __init__(self, client: CLIClient | None = None) -> None:
            super().__init__()
            self._client = client or CLIClient()
            self._accounts: list[dict[str, Any]] = []

        def compose(self) -> ComposeResult:
            yield Header()
            yield DataTable(id="accounts-table")
            yield Static("", id="status-line")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.cursor_type = "row"
            table.add_columns("ref", "enabled", "weight", "auth_present", "quota")
            self.action_refresh()

        def _set_status(self, message: str, *, error: bool = False) -> None:
            line = self.query_one("#status-line", Static)
            line.update(message)
            line.set_class(error, "error")

        def _selected_account(self) -> dict[str, Any] | None:
            if not self._accounts:
                return None
            table = self.query_one(DataTable)
            row = table.cursor_row
            if row is None or row < 0 or row >= len(self._accounts):
                return None
            return self._accounts[row]

        def _render_accounts(self, accounts: list[dict[str, Any]]) -> None:
            table = self.query_one(DataTable)
            selected = self._selected_account()
            selected_ref = selected.get("ref") if selected else None
            table.clear()
            self._accounts = accounts
            for row_index, account in enumerate(accounts):
                table.add_row(
                    account.get("ref"),
                    str(account.get("enabled")),
                    str(account.get("weight")),
                    str(account.get("auth_present")),
                    str(account.get("quota")),
                )
                if selected_ref is not None and account.get("ref") == selected_ref:
                    table.move_cursor(row=row_index)

        def action_refresh(self) -> None:
            self._refresh_accounts()

        @work(exclusive=True)
        async def _refresh_accounts(self) -> None:
            await self._load_accounts()

        async def _load_accounts(self) -> None:
            """Plain coroutine so action workers can await it directly."""
            try:
                data = await self._client.status()
            except CLIError as exc:
                self._set_status(f"error: {exc.message}", error=True)
                return
            accounts = data.get("accounts", [])
            self._render_accounts(accounts)
            self._set_status(
                f"{len(accounts)} account(s), {data.get('eligible_count', 0)} eligible"
            )

        def action_refresh_quota(self) -> None:
            self._refresh_quota()

        @work(exclusive=True)
        async def _refresh_quota(self) -> None:
            try:
                data = await self._client.quota()
            except CLIError as exc:
                self._set_status(f"quota error: {exc.message}", error=True)
                return
            parts = []
            for entry in data.get("accounts", []):
                ref = entry.get("ref")
                quota = entry.get("quota")
                if isinstance(quota, dict):
                    parts.append(
                        f"{ref}: primary={quota.get('primary_used_percent')}% "
                        f"secondary={quota.get('secondary_used_percent')}%"
                    )
                else:
                    parts.append(f"{ref}: {quota}")
            self._set_status(" | ".join(parts) if parts else "no accounts")

        def action_import_account(self) -> None:
            def handle(result: tuple[str, str, int] | None) -> None:
                if result is not None:
                    ref, path, weight = result
                    self._do_import(ref, path, weight)

            self.push_screen(ImportScreen(), handle)

        @work(exclusive=True)
        async def _do_import(self, ref: str, path: str, weight: int) -> None:
            try:
                await self._client.accounts_import(ref, path, weight=weight)
            except CLIError as exc:
                self._set_status(f"import failed: {exc.message}", error=True)
                return
            self._set_status(f"imported {ref}")
            await self._load_accounts()

        def action_login_account(self) -> None:
            account = self._selected_account()
            ref = account["ref"] if account else ""
            self.push_screen(LoginScreen(self._client, ref), lambda _: self.action_refresh())

        def action_toggle_enabled(self) -> None:
            account = self._selected_account()
            if account is None:
                self._set_status("select an account first", error=True)
                return
            self._do_toggle(account["ref"], enabled=account["enabled"])

        @work(exclusive=True)
        async def _do_toggle(self, ref: str, *, enabled: bool) -> None:
            try:
                if enabled:
                    await self._client.pool_disable(ref)
                else:
                    await self._client.pool_enable(ref)
            except CLIError as exc:
                self._set_status(f"toggle failed: {exc.message}", error=True)
                return
            await self._load_accounts()

        def action_weight_up(self) -> None:
            self._adjust_weight(1)

        def action_weight_down(self) -> None:
            self._adjust_weight(-1)

        def _adjust_weight(self, delta: int) -> None:
            account = self._selected_account()
            if account is None:
                self._set_status("select an account first", error=True)
                return
            new_weight = max(1, int(account["weight"]) + delta)
            self._do_set_weight(account["ref"], new_weight)

        def action_set_weight(self) -> None:
            account = self._selected_account()
            if account is None:
                self._set_status("select an account first", error=True)
                return

            def handle(result: int | None) -> None:
                if result is not None:
                    self._do_set_weight(account["ref"], result)

            self.push_screen(WeightScreen(account["ref"], int(account["weight"])), handle)

        @work(exclusive=True)
        async def _do_set_weight(self, ref: str, weight: int) -> None:
            try:
                await self._client.pool_weight(ref, weight)
            except CLIError as exc:
                self._set_status(f"weight change failed: {exc.message}", error=True)
                return
            await self._load_accounts()


def run_tui(client: CLIClient | None = None) -> None:
    """Entry point for ``codex-pool tui`` and bare ``codex-pool``."""
    if _TEXTUAL_IMPORT_ERROR is not None:
        print(f"error: {_missing_textual_error()}", file=sys.stderr)
        raise SystemExit(1)
    CodexPoolApp(client=client).run()


__all__ = ["run_tui"]
