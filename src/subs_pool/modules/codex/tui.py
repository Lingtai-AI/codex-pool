"""Textual TUI — a pure frontend over a selected module's CLI contract.

No account/token storage, auth, or provider HTTP happens in this module:
the shell delegates machine-safe operations to the selected module and renders
returned JSON facts.
"""

from __future__ import annotations

import asyncio
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ...cli_client import CLIClient, CLIError, LoginStream
from ...registry import DEFAULT_MODULE_ID, ModuleDescriptor, get_module


@dataclass
class _QuotaView:
    state: str = "not_checked"
    snapshot: dict[str, Any] | None = None
    attempted_at: str | None = None
    error: str | None = None


def _clean_error(value: Any, fallback: str = "quota unavailable") -> str:
    if isinstance(value, Mapping):
        value = value.get("message")
    if not isinstance(value, str) or not value.strip():
        return fallback
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= 120 else one_line[:119] + "…"


def _remaining_from_used(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    used = float(value)
    if not math.isfinite(used) or not 0.0 <= used <= 100.0:
        return None
    return max(0.0, 100.0 - used)


def _quota_view(value: Any, attempted_at: str) -> _QuotaView:
    if not isinstance(value, Mapping):
        return _QuotaView(
            state="unavailable",
            attempted_at=attempted_at,
            error="malformed quota result",
        )
    snapshot = dict(value)
    backend_status = snapshot.get("status")
    if snapshot.get("error") is not None or backend_status not in (None, "ok"):
        reason = snapshot.get("error")
        if reason is None and backend_status is not None:
            reason = f"quota status {backend_status}"
        return _QuotaView(
            state="unavailable",
            snapshot=snapshot,
            attempted_at=attempted_at,
            error=_clean_error(reason),
        )

    primary = _remaining_from_used(snapshot.get("primary_used_percent"))
    secondary = _remaining_from_used(snapshot.get("secondary_used_percent"))
    if primary is not None:
        state = "ok"
    elif secondary is not None:
        state = "partial"
    else:
        state = "empty"
    return _QuotaView(state=state, snapshot=snapshot, attempted_at=attempted_at)


def _sidecar_sample_view(sample: Any, attempted_at: str | None, *, state: str = "ok", error: Any = None) -> _QuotaView:
    """Adapt a Codex sidecar sample to the table's display model."""
    if not isinstance(sample, Mapping):
        return _QuotaView(state=state, attempted_at=attempted_at, error=_clean_error(error) if error else None)
    primary = sample.get("primary") if isinstance(sample.get("primary"), Mapping) else {}
    secondary = sample.get("secondary") if isinstance(sample.get("secondary"), Mapping) else {}
    flat = {
        "primary_used_percent": primary.get("used_percent"),
        "secondary_used_percent": secondary.get("used_percent"),
        "primary_reset_at": primary.get("reset_at"),
        "secondary_reset_at": secondary.get("reset_at"),
        "primary_window_duration_mins": (primary.get("window_seconds") / 60.0 if isinstance(primary.get("window_seconds"), (int, float)) else None),
        "secondary_window_duration_mins": (secondary.get("window_seconds") / 60.0 if isinstance(secondary.get("window_seconds"), (int, float)) else None),
        "observed_at": sample.get("source_at"),
        "status": "ok" if state in {"ok", "exhausted"} else state,
    }
    view = _quota_view(flat, attempted_at or str(sample.get("checked_at") or ""))
    view.state = state
    view.error = _clean_error(error) if error else view.error
    return view


def _format_percent(value: float) -> str:
    if value == 0.0:
        return "0%"
    if value == 100.0:
        return "100%"
    if value < 0.1:
        return "<0.1%"
    if value > 99.9:
        return ">99.9%"
    if value.is_integer():
        return f"{value:.0f}%"
    return f"{value:.1f}%"


def _bar_parts(value: float, width: int) -> tuple[str, str]:
    if value == 0.0:
        filled = 0
    elif value == 100.0:
        filled = width
    else:
        filled = min(width - 1, max(1, round(value * width / 100.0)))
    return "█" * filled, "░" * (width - filled)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _format_utc(value: Any, *, seconds: bool = True) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "N/A"
    fmt = "%Y-%m-%d %H:%M:%SZ" if seconds else "%H:%MZ"
    return parsed.astimezone(timezone.utc).strftime(fmt)


def _format_reset(value: Any) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "N/A"
    rendered = parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    if parsed.astimezone(timezone.utc) < datetime.now(timezone.utc):
        return f"{rendered} (passed; press u to check)"
    return rendered


def _format_attempt(value: str | None) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "N/A"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def _format_duration(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    minutes = float(value)
    if not math.isfinite(minutes) or minutes < 0.0:
        return "N/A"
    if minutes and minutes % 1440 == 0:
        return f"{minutes / 1440:g}d"
    if minutes and minutes % 60 == 0:
        return f"{minutes / 60:g}h"
    return f"{minutes:g} min"


def _missing_textual_error() -> str:
    return (
        "the subs-pool TUI requires the 'textual' package "
        "(it is a mandatory dependency of subs-pool; reinstall subs-pool "
        "if this is missing)"
    )


try:
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.events import Resize
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Header, Input, Label, Static
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


    class SubscriptionPoolApp(App[None]):
        """Main accounts/status view with pool and auth actions."""

        TITLE = "subs-pool"

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
        #accounts-summary {
            height: 1;
            padding: 0 1;
            color: $text-muted;
            text-align: right;
        }
        #accounts-table {
            height: 1fr;
            min-height: 5;
        }
        #selected-detail {
            height: 8;
            padding: 0 1;
            border-top: solid $accent;
            overflow-y: auto;
        }
        #quota-summary {
            height: 1;
            padding: 0 1;
            color: $text-muted;
        }
        #status-line {
            height: auto;
            min-height: 1;
            max-height: 2;
            padding: 0 1;
        }
        #status-line.error {
            color: $error;
        }
        #action-legend {
            height: 2;
            padding: 0 1;
            color: $text-muted;
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

        def __init__(
            self,
            module: ModuleDescriptor | None = None,
            client: CLIClient | None = None,
        ) -> None:
            super().__init__()
            self._module = module or get_module(DEFAULT_MODULE_ID)
            self.title = f"subs-pool — {self._module.display_name} Accounts"
            if client is not None:
                self._client = client
            elif self._module.id == "codex":
                from .tui_adapter import CodexTUIAdapter
                self._client = CodexTUIAdapter()
            else:
                self._client = CLIClient(self._module.id)
            self._accounts: list[dict[str, Any]] = []
            self._quota_by_ref: dict[str, _QuotaView] = {}
            self._selected_ref: str | None = None
            self._eligible_count = 0
            self._quota_running = False
            self._status_source: str | None = None
            self._last_quota_refresh = 0.0

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="accounts-summary")
            yield DataTable(id="accounts-table")
            yield Static("", id="selected-detail")
            yield Static("", id="quota-summary")
            yield Static("", id="status-line")
            yield Static(
                "q Quit  i Import  l Login  e Enable/Disable  w Set weight\n"
                "+/- Weight  u Quota (all)  r Refresh accounts  ↑/↓ Select",
                id="action-legend",
            )

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = True
            table.add_column("REF", key="ref")
            table.add_column("EN", key="enabled")
            table.add_column("WT", key="weight")
            table.add_column("AUTH", key="auth_present")
            table.add_column("P REMAINING", key="primary")
            table.add_column("S REMAINING", key="secondary")
            table.add_column("CHECK", key="check")
            table.focus()
            self.set_interval(1.0, self._quota_tick)
            self.set_interval(1.0, self._inspect_shared_state)
            self.action_refresh()

        def _quota_tick(self) -> None:
            """Inspect the shared view and request the 30-second target."""
            if self._quota_running or not self._accounts:
                return
            if asyncio.get_running_loop().time() - self._last_quota_refresh >= 30.0:
                self.action_refresh_quota()

        @work(exclusive=True, group="observe")
        async def _inspect_shared_state(self) -> None:
            if self._quota_running or not self._accounts:
                return
            try:
                data = await self._client.status()
            except CLIError as exc:
                for ref in self._quota_by_ref:
                    self._quota_by_ref[ref] = _QuotaView(state="unavailable", error=_clean_error(exc.message, "state unavailable"))
                if self.is_mounted:
                    self._render_quota_rows()
                    self._render_detail()
                    self._render_quota_summary()
                return
            rows = data.get("accounts") if isinstance(data, Mapping) else None
            if not isinstance(rows, list):
                return
            by_ref = {row.get("ref"): row for row in rows if isinstance(row, Mapping) and isinstance(row.get("ref"), str)}
            for ref in set(self._quota_by_ref) - set(by_ref):
                # A successful observation that omits a previously displayed
                # account must not leave its old green values on screen.
                self._quota_by_ref[ref] = _QuotaView(state="unavailable", error="account state changed")
            for ref, row in by_ref.items():
                freshness = row.get("freshness")
                if freshness == "checking" and row.get("enabled") and row.get("authenticated"):
                    self._quota_by_ref[ref] = _QuotaView(state="checking", attempted_at=row.get("attempted_at"))
                elif freshness == "stale":
                    self._quota_by_ref[ref] = _sidecar_sample_view(row.get("last_success"), row.get("attempted_at"), state="stale")
                elif freshness == "failed":
                    self._quota_by_ref[ref] = _sidecar_sample_view(None, row.get("attempted_at"), state="unavailable", error=row.get("error"))
                elif freshness == "fresh" and row.get("eligible"):
                    self._quota_by_ref[ref] = _sidecar_sample_view(row.get("current"), row.get("attempted_at"), state="ok", error=row.get("error"))
                elif freshness == "fresh":
                    if row.get("exclusion_reason") == "exhausted" and isinstance(row.get("current"), Mapping):
                        self._quota_by_ref[ref] = _sidecar_sample_view(row.get("current"), row.get("attempted_at"), state="exhausted", error=row.get("error"))
                    else:
                        self._quota_by_ref[ref] = _sidecar_sample_view(None, row.get("attempted_at"), state="unavailable", error=row.get("exclusion_reason") or row.get("error"))
                else:
                    self._quota_by_ref[ref] = _sidecar_sample_view(None, row.get("attempted_at"), state="unavailable", error=row.get("exclusion_reason") or row.get("error"))
            if self.is_mounted:
                self._render_quota_rows()
                self._render_detail()
                self._render_quota_summary()

        def _set_status(
            self,
            message: str,
            *,
            error: bool = False,
            source: str = "management",
        ) -> None:
            line = self.query_one("#status-line", Static)
            line.update(Text(message))
            line.set_class(error, "error")
            self._status_source = source

        def _selected_account(self) -> dict[str, Any] | None:
            return next(
                (
                    account
                    for account in self._accounts
                    if account.get("ref") == self._selected_ref
                ),
                None,
            )

        def _table_meter_width(self) -> int:
            return 10 if self.size.width >= 100 else 6

        def _detail_meter_width(self) -> int:
            return 20 if self.size.width >= 100 else 10

        def _clip_ref(self, ref: str) -> str:
            limit = 22 if self.size.width >= 100 else 12
            return ref if len(ref) <= limit else ref[: limit - 1] + "…"

        def _bool_text(self, value: Any) -> str:
            if value is not True and value is not False:
                return "N/A"
            if self.size.width >= 100:
                return "yes" if value else "no"
            return "Y" if value else "N"

        def _remaining(self, view: _QuotaView, kind: str) -> float | None:
            if view.state not in {"ok", "partial", "empty", "exhausted"} or view.snapshot is None:
                return None
            return _remaining_from_used(view.snapshot.get(f"{kind}_used_percent"))

        def _meter(
            self,
            remaining: float | None,
            width: int,
            *,
            say_remaining: bool = False,
        ) -> Text:
            if remaining is None:
                return Text("N/A", style="dim")
            color = "bright_green" if remaining > 20.0 else "bright_yellow" if remaining > 0.0 else "bright_red"
            filled, empty = _bar_parts(remaining, width)
            meter = Text()
            meter.append(_format_percent(remaining), style=f"bold {color}")
            meter.append(" remaining" if say_remaining else "")
            meter.append(" [", style="dim")
            meter.append(filled, style=color)
            meter.append(empty, style="grey50")
            meter.append("]", style="dim")
            return meter

        def _window_meter(
            self,
            view: _QuotaView,
            kind: str,
            width: int,
            *,
            say_remaining: bool = False,
        ) -> Text:
            return self._meter(
                self._remaining(view, kind),
                width,
                say_remaining=say_remaining,
            )

        def _observed(self, view: _QuotaView, *, seconds: bool = True) -> str:
            snapshot = view.snapshot
            return _format_utc(
                snapshot.get("observed_at") if snapshot is not None else None,
                seconds=seconds,
            )

        def _check_cell(self, view: _QuotaView) -> Text:
            observed = self._observed(view, seconds=False)
            suffix = f" {observed}" if observed != "N/A" else ""
            if view.state == "not_checked":
                return Text("CHECKING", style="bold yellow")
            if view.state == "checking":
                return Text("CHECKING", style="bold yellow")
            if view.state == "ok":
                return Text(f"OK{suffix}", style="bright_green")
            if view.state == "exhausted":
                return Text(f"EXHAUSTED{suffix}", style="bright_red")
            if view.state == "stale":
                return Text("STALE", style="bright_yellow")
            if view.state == "partial":
                return Text(f"PARTIAL{suffix}", style="bright_yellow")
            if view.state == "empty":
                return Text("NO WINDOW DATA", style="dim")
            if view.state == "interrupted":
                return Text("INTERRUPTED", style="bright_red")
            reason = view.error or ""
            if reason.startswith("http_status_"):
                return Text(f"UNAVAIL {reason.removeprefix('http_status_')}", style="bright_red")
            return Text("UNAVAILABLE", style="bright_red")

        def _row_cells(self, account: dict[str, Any]) -> tuple[Any, ...]:
            ref = str(account["ref"])
            view = self._quota_by_ref.get(ref, _QuotaView())
            width = self._table_meter_width()
            return (
                Text(self._clip_ref(ref)),
                self._bool_text(account.get("enabled")),
                str(account.get("weight", "N/A")),
                self._bool_text(account.get("auth_present")),
                self._window_meter(view, "primary", width),
                self._window_meter(view, "secondary", width),
                self._check_cell(view),
            )

        def _render_accounts(self, accounts: list[dict[str, Any]]) -> None:
            table = self.query_one(DataTable)
            selected_ref = self._selected_ref
            refs = [str(account["ref"]) for account in accounts]
            previous_quota = self._quota_by_ref
            self._quota_by_ref = {
                ref: previous_quota.get(ref, _QuotaView()) for ref in refs
            }
            self._accounts = accounts
            table.clear()
            for account in accounts:
                ref = str(account["ref"])
                table.add_row(*self._row_cells(account), key=ref)

            if selected_ref not in refs:
                selected_ref = refs[0] if refs else None
            self._selected_ref = selected_ref
            if selected_ref is not None:
                table.move_cursor(row=refs.index(selected_ref))
            self._render_detail()
            self._render_quota_summary()

        def _render_quota_rows(self, refs: tuple[str, ...] | None = None) -> None:
            table = self.query_one(DataTable)
            requested = set(refs) if refs is not None else None
            width = self._table_meter_width()
            for account in self._accounts:
                ref = str(account["ref"])
                if requested is not None and ref not in requested:
                    continue
                view = self._quota_by_ref.get(ref, _QuotaView())
                table.update_cell(
                    ref,
                    "primary",
                    self._window_meter(view, "primary", width),
                    update_width=True,
                )
                table.update_cell(
                    ref,
                    "secondary",
                    self._window_meter(view, "secondary", width),
                    update_width=True,
                )
                table.update_cell(ref, "check", self._check_cell(view), update_width=True)

        def _window_detail(self, detail: Text, view: _QuotaView, kind: str) -> None:
            snapshot = view.snapshot if view.state in {"ok", "partial", "empty", "exhausted"} else None
            title = kind.capitalize()
            if snapshot is not None:
                name = snapshot.get(f"{kind}_window_name")
                if isinstance(name, str) and name:
                    title += f" — {name}"
            detail.append(f"{title}  ", style="bold")
            detail.append_text(
                self._window_meter(
                    view,
                    kind,
                    self._detail_meter_width(),
                    say_remaining=True,
                )
            )
            duration = _format_duration(
                snapshot.get(f"{kind}_window_duration_mins")
                if snapshot is not None
                else None
            )
            detail.append(f"  Duration {duration}\n", style="dim")
            reset = _format_reset(
                snapshot.get(f"{kind}_reset_at") if snapshot is not None else None
            )
            detail.append(f"  Reset {reset}\n", style="dim")

        def _detail_check(self, view: _QuotaView) -> tuple[str, str]:
            if view.state == "not_checked":
                return "CHECKING", "Checking quota…"
            if view.state == "checking":
                return "CHECKING", "Checking quota…"
            if view.state == "ok":
                return "OK", ""
            if view.state == "exhausted":
                return "EXHAUSTED", "The current quota window is exhausted."
            if view.state == "stale":
                return "STALE", "The last successful check is historical."
            if view.state == "partial":
                return "PARTIAL", "One window was not provided."
            if view.state == "empty":
                return "NO WINDOW DATA", "No quota windows were provided."
            if view.state == "interrupted":
                return "INTERRUPTED", view.error or "Quota check was interrupted."
            return "UNAVAILABLE", view.error or "quota unavailable"

        def _render_detail(self) -> None:
            pane = self.query_one("#selected-detail", Static)
            account = self._selected_account()
            if account is None:
                pane.update(Text("No accounts — i Import / l Login", style="dim"))
                return

            ref = str(account["ref"])
            view = self._quota_by_ref.get(ref, _QuotaView())
            detail = Text()
            detail.append("SELECTED ", style="bold cyan")
            detail.append(ref, style="bold")
            detail.append(
                f"  Enabled {self._bool_text(account.get('enabled'))}"
                f" | Weight {account.get('weight', 'N/A')}"
                f" | Auth present {self._bool_text(account.get('auth_present'))}\n"
            )
            self._window_detail(detail, view, "primary")
            self._window_detail(detail, view, "secondary")
            check, explanation = self._detail_check(view)
            style = (
                "bright_green"
                if check == "OK"
                else "bright_yellow"
                if check in {"CHECKING", "PARTIAL"}
                else "bright_red"
                if check in {"UNAVAILABLE", "INTERRUPTED"}
                else "dim"
            )
            detail.append("Check ", style="bold")
            detail.append(check, style=f"bold {style}")
            if explanation:
                detail.append(f" — {explanation}")
            detail.append(f" | Observed {self._observed(view)}\n")
            detail.append(f"Last attempt {_format_attempt(view.attempted_at)} (local)", style="dim")
            pane.update(detail)

        def _render_quota_summary(self) -> None:
            counts = {state: 0 for state in ("ok", "unavailable", "not_checked", "checking", "stale", "exhausted")}
            for ref in (str(account["ref"]) for account in self._accounts):
                state = self._quota_by_ref.get(ref, _QuotaView()).state
                if state in {"ok", "partial", "empty"}:
                    counts["ok"] += 1
                elif state == "exhausted":
                    counts["exhausted"] += 1
                elif state == "stale":
                    counts["stale"] += 1
                elif state in {"unavailable", "interrupted"}:
                    counts["unavailable"] += 1
                elif state == "checking":
                    counts["checking"] += 1
                else:
                    counts["not_checked"] += 1
            parts = [
                f"Quota: {counts['ok']} OK",
                f"{counts['unavailable']} unavailable",
                f"{counts['exhausted']} exhausted",
                f"{counts['stale']} stale",
                f"{counts['not_checked']} pending",
            ]
            if counts["checking"]:
                parts.append(f"{counts['checking']} checking")
            self.query_one("#quota-summary", Static).update(Text(" | ".join(parts) + "."))

        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            ref = str(event.row_key.value)
            if any(account.get("ref") == ref for account in self._accounts):
                self._selected_ref = ref
                self._render_detail()

        def on_resize(self, event: Resize) -> None:
            table = self.query_one(DataTable)
            if table.row_count == 0:
                return
            for account in self._accounts:
                ref = str(account["ref"])
                table.update_cell(ref, "ref", Text(self._clip_ref(ref)), update_width=True)
                table.update_cell(
                    ref,
                    "enabled",
                    self._bool_text(account.get("enabled")),
                    update_width=True,
                )
                table.update_cell(
                    ref,
                    "auth_present",
                    self._bool_text(account.get("auth_present")),
                    update_width=True,
                )
            self._render_quota_rows()
            self._render_detail()

        def action_refresh(self) -> None:
            self._refresh_accounts()

        @work(exclusive=True, group="metadata")
        async def _refresh_accounts(self) -> None:
            await self._load_accounts()

        async def _load_accounts(self) -> None:
            """Plain coroutine so action workers can await it directly."""
            try:
                data = await self._client.status()
            except CLIError as exc:
                self._set_status(
                    f"account refresh failed: {exc.message}",
                    error=True,
                    source="metadata",
                )
                return
            raw_accounts = data.get("accounts")
            if not isinstance(raw_accounts, list) or not all(
                isinstance(account, dict) and isinstance(account.get("ref"), str)
                for account in raw_accounts
            ):
                self._set_status(
                    "account refresh failed: malformed account data",
                    error=True,
                    source="metadata",
                )
                return
            accounts = raw_accounts
            eligible_count = data.get("eligible_count", 0)
            self._eligible_count = (
                eligible_count
                if isinstance(eligible_count, int) and not isinstance(eligible_count, bool)
                else 0
            )
            self._render_accounts(accounts)
            self.query_one("#accounts-summary", Static).update(
                Text(f"{len(accounts)} account(s) | {self._eligible_count} eligible")
            )
            if self._status_source in (None, "metadata"):
                message = (
                    "No accounts — i Import / l Login"
                    if not accounts
                    else f"{len(accounts)} account(s), {self._eligible_count} eligible; "
                    "r reloads accounts and checks quota, u forces quota check"
                )
                self._set_status(message, source="metadata")
            for account in accounts:
                ref = str(account["ref"])
                if not (account.get("enabled") and account.get("authenticated")):
                    self._quota_by_ref[ref] = _QuotaView(
                        state="unavailable", error=str(account.get("exclusion_reason") or "unavailable")
                    )
            # Startup and metadata reload both enter the explicit CHECKING
            # state before showing any current values for checkable accounts.
            self.action_refresh_quota()

        def action_refresh_quota(self) -> None:
            if self._quota_running:
                self._set_status("Quota check already running", source="quota")
                return
            requested_refs = tuple(str(account["ref"]) for account in self._accounts)
            if not requested_refs:
                self._set_status("No accounts — i Import / l Login", source="quota")
                return

            attempted_at = datetime.now().astimezone().isoformat()
            self._quota_running = True
            self._last_quota_refresh = asyncio.get_running_loop().time()
            for ref in requested_refs:
                account = next((item for item in self._accounts if item.get("ref") == ref), {})
                self._quota_by_ref[ref] = _QuotaView(
                    state="checking" if account.get("enabled") and account.get("authenticated") else "unavailable",
                    attempted_at=attempted_at,
                    error=None if account.get("enabled") and account.get("authenticated") else str(account.get("exclusion_reason") or "unavailable"),
                )
            self._render_quota_rows(requested_refs)
            self._render_detail()
            self._render_quota_summary()
            self._set_status(
                f"Checking quota for {len(requested_refs)} account(s)…",
                source="quota",
            )
            self._refresh_quota(requested_refs, attempted_at)

        @work(exclusive=True, group="quota")
        async def _refresh_quota(
            self,
            requested_refs: tuple[str, ...],
            attempted_at: str,
        ) -> None:
            try:
                data = await self._client.quota()
            except CLIError as exc:
                reason = _clean_error(exc.message)
                if isinstance(exc.data, dict):
                    data = exc.data
                    partial_error = reason
                else:
                    for ref in requested_refs:
                        if ref in self._quota_by_ref:
                            self._quota_by_ref[ref] = _QuotaView(state="unavailable", attempted_at=attempted_at, error=reason)
                    if self.is_mounted:
                        self._render_quota_rows(requested_refs)
                        self._render_detail()
                        self._render_quota_summary()
                        self._set_status(f"Quota check unavailable: {reason}", error=True, source="quota")
                    return
            except asyncio.CancelledError:
                for ref in requested_refs:
                    current = self._quota_by_ref.get(ref)
                    if current is not None and current.state == "checking":
                        self._quota_by_ref[ref] = _QuotaView(
                            state="interrupted",
                            attempted_at=attempted_at,
                            error="Quota check was interrupted.",
                        )
                # Textual cancels workers while tearing down the widget tree.
                # Preserve the interrupted state for any later normal render,
                # but never query widgets from the cancellation path.
                raise
            except Exception as exc:  # noqa: BLE001 - keep the TUI fail-soft
                reason = f"unexpected {type(exc).__name__}"
                for ref in requested_refs:
                    if ref in self._quota_by_ref:
                        self._quota_by_ref[ref] = _QuotaView(
                            state="unavailable",
                            attempted_at=attempted_at,
                            error=reason,
                        )
                if self.is_mounted:
                    self._render_quota_rows(requested_refs)
                    self._render_detail()
                    self._render_quota_summary()
                    self._set_status(
                        f"Quota check unavailable: {reason}",
                        error=True,
                        source="quota",
                    )
                return
            finally:
                self._quota_running = False

            partial_error = locals().get("partial_error")
            raw_entries = data.get("accounts") if isinstance(data, dict) else None
            entries: dict[str, Any] = {}
            if isinstance(raw_entries, list):
                for entry in raw_entries:
                    if isinstance(entry, Mapping) and isinstance(entry.get("ref"), str):
                        current = entry.get("current")
                        if isinstance(current, Mapping):
                            primary = current.get("primary") if isinstance(current.get("primary"), Mapping) else {}
                            secondary = current.get("secondary") if isinstance(current.get("secondary"), Mapping) else {}
                            entries[entry["ref"]] = {
                                "primary_used_percent": primary.get("used_percent"),
                                "secondary_used_percent": secondary.get("used_percent"),
                                "primary_reset_at": primary.get("reset_at"),
                                "secondary_reset_at": secondary.get("reset_at"),
                                "primary_window_duration_mins": (primary.get("window_seconds") / 60.0 if isinstance(primary.get("window_seconds"), (int, float)) else None),
                                "secondary_window_duration_mins": (secondary.get("window_seconds") / 60.0 if isinstance(secondary.get("window_seconds"), (int, float)) else None),
                                "observed_at": current.get("source_at"),
                                "status": "ok",
                                "error": None,
                                "_exhausted": entry.get("exclusion_reason") == "exhausted",
                            }
                        elif entry.get("freshness") == "stale":
                            entries[entry["ref"]] = {"status": "stale", "error": "last successful check is stale"}
                        elif entry.get("error") is not None or entry.get("exclusion_reason") is not None:
                            entries[entry["ref"]] = {"status": "unavailable", "error": entry.get("error") or entry.get("exclusion_reason")}
                        else:
                            entries[entry["ref"]] = {"status": entry.get("freshness", "never"), "error": entry.get("exclusion_reason")}

            for ref in requested_refs:
                if ref not in self._quota_by_ref:
                    continue
                if ref not in entries:
                    self._quota_by_ref[ref] = _QuotaView(
                        state="unavailable",
                        attempted_at=attempted_at,
                        error="quota result missing",
                    )
                else:
                    entry = entries[ref]
                    view = _quota_view(entry, attempted_at)
                    if isinstance(entry, Mapping) and entry.get("_exhausted"):
                        view.state = "exhausted"
                    if isinstance(entry, Mapping) and entry.get("status") == "stale":
                        view.state = "stale"
                    self._quota_by_ref[ref] = view

            if self.is_mounted:
                self._render_quota_rows(requested_refs)
                self._render_detail()
                self._render_quota_summary()
                current_refs = [ref for ref in requested_refs if ref in self._quota_by_ref]
                unavailable = sum(
                    self._quota_by_ref[ref].state in {"unavailable", "interrupted"}
                    for ref in current_refs
                )
                self._set_status(
                    f"Quota check complete: {len(current_refs) - unavailable} current, "
                    f"{unavailable} unavailable",
                    error=bool(unavailable) or bool(partial_error),
                    source="quota",
                )

        def action_import_account(self) -> None:
            def handle(result: tuple[str, str, int] | None) -> None:
                if result is not None:
                    ref, path, weight = result
                    self._do_import(ref, path, weight)

            self.push_screen(ImportScreen(), handle)

        @work(exclusive=True, group="management")
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

        @work(exclusive=True, group="management")
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

        @work(exclusive=True, group="management")
        async def _do_set_weight(self, ref: str, weight: int) -> None:
            try:
                await self._client.pool_weight(ref, weight)
            except CLIError as exc:
                self._set_status(f"weight change failed: {exc.message}", error=True)
                return
            await self._load_accounts()


def run_tui(
    module_id: str = DEFAULT_MODULE_ID,
    client: CLIClient | None = None,
) -> None:
    """Run the shared account frontend for one explicit built-in module."""
    if _TEXTUAL_IMPORT_ERROR is not None:
        print(f"error: {_missing_textual_error()}", file=sys.stderr)
        raise SystemExit(1)
    module = get_module(module_id)
    SubscriptionPoolApp(module=module, client=client).run()


__all__ = ["SubscriptionPoolApp", "run_tui"]
