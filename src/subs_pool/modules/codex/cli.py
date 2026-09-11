"""Human Codex command surface and shared operation handlers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
import threading
from typing import Any

import httpx

from ...output import envelope, error_object
from .accounts import AccountError, AccountStore
from .auth_codex import CodexTokenManager
from .quota_refresh import QuotaRefreshCoordinator
from .quota_store import QuotaStateError, QuotaStore, iso

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class OperationError(Exception):
    def __init__(self, code: str, message: str, *, exit_code: int = 4, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details if details is not None else {}


def _store() -> AccountStore:
    try:
        return AccountStore()
    except (OSError, ValueError) as exc:
        raise OperationError("invalid_root", "selected Codex root is invalid", exit_code=4) from exc


def _human_error(exc: BaseException) -> int:
    if isinstance(exc, OperationError):
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    if isinstance(exc, AccountError):
        print(f"error: {exc}", file=sys.stderr)
        return 4
    print("error: unexpected internal failure", file=sys.stderr)
    return 5


def account_list_data(store: AccountStore | None = None) -> dict[str, Any]:
    store = store or _store()
    try:
        accounts = store.list()
    except AccountError as exc:
        raise OperationError("state_unavailable", "account state is unavailable", exit_code=4) from exc
    return {"accounts": [account.to_status_dict(root=store.root) for account in accounts]}


def status_data(store: AccountStore | None = None, quota_store: QuotaStore | None = None) -> dict[str, Any]:
    store = store or _store()
    quota_store = quota_store or QuotaStore(store.root)
    try:
        accounts, snapshot = quota_store.read_consistent(store)
    except (AccountError, QuotaStateError, OSError) as exc:
        raise OperationError("state_unavailable", "pool state is unavailable", exit_code=4) from exc
    rows = []
    for account in accounts:
        row = account.to_status_dict(root=store.root)
        view = quota_store.view_for(account, snapshot)
        row.update({
            "freshness": view["freshness"],
            "eligible": view["eligible"],
            "exclusion_reason": view["exclusion_reason"],
            "status": view["status"],
            "attempted_at": view["attempted_at"],
            "checked_at": view["checked_at"],
            "current": view["current"],
            "last_success": view["last_success"],
            "error": view["error"],
        })
        rows.append(row)
    return {"accounts": rows, "eligible_count": sum(bool(row["eligible"]) for row in rows)}


def quota_data(
    store: AccountStore | None = None,
    *,
    coordinator: QuotaRefreshCoordinator | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, Any], int]:
    store = store or _store()
    coordinator = coordinator or QuotaRefreshCoordinator(store)
    outcome = coordinator.refresh(force=True, cancel_event=cancel_event)
    quota_store = coordinator.store
    try:
        accounts, snapshot = quota_store.read_consistent(store)
    except (AccountError, QuotaStateError, OSError) as exc:
        raise OperationError("state_unavailable", "quota state is unavailable", exit_code=4) from exc
    rows = quota_store.rows(accounts, snapshot, now=coordinator._now(), unsatisfied_refs=set(outcome.unsatisfied_refs))
    data = {
        "module": "codex",
        "codex_root": str(store.root),
        "generated_at": iso(coordinator._now()),
        "snapshot_revision": snapshot.get("revision"),
        "refresh": outcome.to_dict(),
        "eligible_count": sum(bool(row["eligible"]) for row in rows),
        "accounts": rows,
    }
    if outcome.status in {"completed", "coalesced"} and outcome.failed == 0 and not outcome.unsatisfied_refs:
        return data, 0
    return data, 3


def _print_human_quota(data: dict[str, Any]) -> None:
    if not data["accounts"]:
        print("(no accounts imported)")
    for row in data["accounts"]:
        current = row.get("current")
        if isinstance(current, dict):
            primary = current.get("primary", {}).get("remaining_percent")
            secondary = current.get("secondary", {}).get("remaining_percent")
            check = "EXHAUSTED" if not row["eligible"] else "CHECK OK"
            print(f"{row['ref']:<20} primary_remaining={primary!s:<8} secondary_remaining={secondary!s:<8} {check}")
        else:
            print(f"{row['ref']:<20} {str(row.get('exclusion_reason') or row.get('freshness')).upper()}")
    refresh = data.get("refresh", {})
    if refresh.get("status") not in {"completed", "coalesced"}:
        print(f"quota refresh: {refresh.get('status', 'unavailable')}", file=sys.stderr)


def _cmd_import(args: argparse.Namespace) -> int:
    store = _store()
    account = store.import_account(args.ref, args.path, weight=args.weight)
    print(f"imported {account.ref}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    data = account_list_data()
    for row in data["accounts"]:
        print(f"{row['ref']:<20} enabled={row['enabled']!s:<5} weight={row['weight']:<3} auth={row['auth_present']!s:<5}")
    if not data["accounts"]:
        print("(no accounts imported)")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    if not args.device:
        raise OperationError("prompt_required", "device login requires the human --device flow", exit_code=2)
    from .device_login import DeviceLoginError, run_device_login

    jsonl = bool(getattr(args, "events_jsonl", False))
    try:
        with httpx.Client(trust_env=False) as client:
            for event in run_device_login(args.ref, client=client, weight=args.weight):
                if jsonl:
                    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
                elif event["event"] == "authorization_required":
                    print(f"Open {event['verification_uri']} and enter code: {event['user_code']}")
                    print(f"Waiting for approval (up to {event['expires_in']}s)…")
                elif event["event"] == "completed":
                    print(f"logged in {event['account']['ref']}")
    except (DeviceLoginError, AccountError) as exc:
        raise OperationError("auth_unavailable", str(exc), exit_code=3) from exc
    return 0


def _cmd_toggle(args: argparse.Namespace, enabled: bool) -> int:
    account = _store().set_enabled(args.ref, enabled)
    print(f"{'enabled' if enabled else 'disabled'} {account.ref}")
    return 0


def _cmd_weight(args: argparse.Namespace) -> int:
    account = _store().set_weight(args.ref, args.weight)
    print(f"weight({account.ref})={account.weight}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    data = status_data()
    print(f"subs-pool / Codex — {len(data['accounts'])} account(s), {data['eligible_count']} eligible")
    for row in data["accounts"]:
        print(f"  {row['ref']:<20} enabled={row['enabled']!s:<5} authenticated={row['authenticated']!s:<5} quota={row['freshness']}")
    return 0


def _cmd_quota(args: argparse.Namespace) -> int:
    data, rc = quota_data()
    _print_human_quota(data)
    return rc


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    api_key = args.api_key or os.environ.get("CODEX_POOL_API_KEY")
    if not api_key:
        raise OperationError("invalid_configuration", "--api-key is required (or set CODEX_POOL_API_KEY)", exit_code=4)
    host, separator, port_text = args.listen.rpartition(":")
    if not separator:
        raise OperationError("invalid_configuration", "--listen must be HOST:PORT", exit_code=4)
    host = host or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        raise OperationError("invalid_configuration", "--listen host must be loopback", exit_code=4)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise OperationError("invalid_configuration", "--listen port must be an integer", exit_code=4) from exc
    if not 1 <= port <= 65535:
        raise OperationError("invalid_configuration", "--listen port must be 1-65535", exit_code=4)
    raw_max = os.environ.get("CODEX_POOL_MAX_SESSIONS")
    from .chain import DEFAULT_MAX_SESSIONS, ChainStore
    from .server import create_app
    from .upstream import CodexHTTPUpstream

    if raw_max is None:
        max_sessions = DEFAULT_MAX_SESSIONS
    else:
        try:
            max_sessions = int(raw_max)
        except ValueError as exc:
            raise OperationError("invalid_configuration", "CODEX_POOL_MAX_SESSIONS must be a positive integer", exit_code=4) from exc
        if max_sessions < 1:
            raise OperationError("invalid_configuration", "CODEX_POOL_MAX_SESSIONS must be a positive integer", exit_code=4)
    store = _store()
    app = create_app(accounts=store, chain_store=ChainStore(max_records=max_sessions), upstream=CodexHTTPUpstream(), api_key=api_key)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subspool codex", description="Codex subscription pool")
    sub = parser.add_subparsers(dest="command")
    account = sub.add_parser("account", help="manage Codex accounts")
    account_sub = account.add_subparsers(dest="account_command")
    imp = account_sub.add_parser("import", help="reference an existing auth JSON file")
    imp.add_argument("ref")
    imp.add_argument("--path", required=True)
    imp.add_argument("--weight", type=int, default=1)
    imp.set_defaults(func=_cmd_import)
    lst = account_sub.add_parser("list", help="list account metadata")
    lst.set_defaults(func=_cmd_list)
    login = account_sub.add_parser("login", help="run human device login")
    login.add_argument("ref")
    login.add_argument("--device", action="store_true")
    login.add_argument("--weight", type=int, default=1)
    # Internal TUI transport. It is deliberately hidden from human help and
    # is not exposed by the machine executable; the public human operation
    # remains ``account login ID --device``.
    login.add_argument("--events-jsonl", action="store_true", help=argparse.SUPPRESS)
    login.set_defaults(func=_cmd_login)
    enable = account_sub.add_parser("enable")
    enable.add_argument("ref")
    enable.set_defaults(func=lambda args: _cmd_toggle(args, True))
    disable = account_sub.add_parser("disable")
    disable.add_argument("ref")
    disable.set_defaults(func=lambda args: _cmd_toggle(args, False))
    weight = account_sub.add_parser("weight")
    weight.add_argument("ref")
    weight.add_argument("weight", type=int)
    weight.set_defaults(func=_cmd_weight)
    status = sub.add_parser("status", help="show current local status")
    status.set_defaults(func=_cmd_status)
    quota = sub.add_parser("quota", help="refresh and show shared quota")
    quota.set_defaults(func=_cmd_quota)
    serve = sub.add_parser("serve", help="run the foreground loopback Responses proxy")
    serve.add_argument("--listen", default="127.0.0.1:8765")
    serve.add_argument("--api-key")
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "account" and getattr(args, "account_command", None) is None:
        # Account-level help is human text and performs no state access.
        print("usage: subspool codex account {import,list,login,enable,disable,weight}")
        return 0
    try:
        return args.func(args)
    except (OperationError, AccountError) as exc:
        return _human_error(exc)
    except KeyboardInterrupt:
        return 130
    except Exception:
        return _human_error(RuntimeError())


def machine_operation(argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Execute one Agent operation without printing or prompting."""
    args = list(argv)
    if args and args[0] == "codex":
        args = args[1:]
    if not args or args == ["--help"]:
        return 0, {"usage": "subspool-cli codex account|status|quota"}
    if args == ["account"] or args == ["account", "--help"]:
        return 0, {"usage": "subspool-cli codex account import|list|enable|disable|weight"}
    if args == ["status", "--help"]:
        return 0, {"usage": "subspool-cli codex status"}
    if args == ["status"]:
        try:
            return 0, status_data()
        except OperationError as exc:
            return exc.exit_code, {"error": error_object(exc.code, exc.message, details=exc.details)}
    if args == ["quota"] or args == ["quota", "--help"]:
        if args[-1] == "--help":
            return 0, {"usage": "subspool-cli codex quota"}
        try:
            data, rc = quota_data()
            return rc, data
        except OperationError as exc:
            return exc.exit_code, {"error": error_object(exc.code, exc.message, details=exc.details)}
    if args and args[0] == "account" and len(args) == 3 and args[2] == "--help" and args[1] in {"import", "list", "login", "enable", "disable", "weight"}:
        return 0, {"usage": f"subspool-cli codex account {args[1]}"}
    if args and args[0] == "serve":
        return 2, {"error": error_object("unsupported_command", "serve is human-only; run subspool codex serve")}
    if args[:2] == ["account", "login"]:
        return 2, {"error": error_object("prompt_required", "device login is human-only; run subspool codex account login ID --device")}
    if len(args) >= 2 and args[0] == "account":
        command = args[1]
        try:
            if command == "list" and len(args) == 2:
                return 0, account_list_data()
            if command in {"enable", "disable"} and len(args) == 3:
                store = _store()
                account = store.set_enabled(args[2], command == "enable")
                return 0, account.to_status_dict(root=store.root)
            if command == "weight" and len(args) == 4:
                try:
                    weight = int(args[3])
                except (TypeError, ValueError) as exc:
                    raise OperationError("invalid_syntax", "account weight requires an integer", exit_code=2) from exc
                store = _store()
                account = store.set_weight(args[2], weight)
                return 0, account.to_status_dict(root=store.root)
            if command == "import":
                if len(args) < 3:
                    raise OperationError("invalid_syntax", "account import requires REF --path VALUE", exit_code=2)
                ref = args[2]
                tail = args[3:]
                values: dict[str, str] = {}
                index = 0
                while index < len(tail):
                    option = tail[index]
                    if option not in {"--path", "--weight"} or option in values or index + 1 >= len(tail) or tail[index + 1].startswith("--"):
                        raise OperationError("invalid_syntax", "unsupported or incomplete account import option", exit_code=2)
                    values[option] = tail[index + 1]
                    index += 2
                if "--path" not in values:
                    raise OperationError("invalid_syntax", "account import requires --path VALUE", exit_code=2)
                try:
                    weight = int(values.get("--weight", "1"))
                except (TypeError, ValueError) as exc:
                    raise OperationError("invalid_syntax", "account import weight must be an integer", exit_code=2) from exc
                store = _store()
                account = store.import_account(ref, values["--path"], weight=weight)
                return 0, account.to_status_dict(root=store.root)
        except OperationError as exc:
            return exc.exit_code, {"error": error_object(exc.code, exc.message, details=exc.details)}
        except AccountError as exc:
            return 4, {"error": error_object("state_unavailable", str(exc))}
    return 2, {"error": error_object("invalid_syntax", "unsupported Codex command")}


__all__ = ["account_list_data", "build_parser", "machine_operation", "main", "quota_data", "status_data"]
