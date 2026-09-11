"""Codex Pool CLI — the single operation surface; the future TUI is only a frontend for this.

Every subcommand supports ``--json`` for stable, decorative-output-free
machine consumption and exits non-zero on error. See CLI_CONTRACT.md for the
frozen command/JSON shape this pass hands to the next Textual/TUI work.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from .accounts import AccountError, AccountStore
from .auth_codex import CodexTokenManager

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class _JSONArgumentParser(argparse.ArgumentParser):
    """Argparse that emits ``{"error": ...}`` on stderr for parse errors when ``--json`` was requested.

    ``add_subparsers()`` defaults ``parser_class`` to ``type(self)``, so every
    subparser created off a ``_JSONArgumentParser`` is also one — the hint
    just needs to be set on all of them before ``parse_args`` runs (see
    ``_set_json_hint``).
    """

    def error(self, message: str) -> None:
        if getattr(self, "_json_hint", False):
            print(json.dumps({"error": message}), file=sys.stderr)
            raise SystemExit(2)
        super().error(message)


def _set_json_hint(parser: argparse.ArgumentParser, hint: bool) -> None:
    parser._json_hint = hint  # type: ignore[attr-defined]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _set_json_hint(sub, hint)


def _print(obj: dict, *, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(human if human is not None else json.dumps(obj, indent=2, sort_keys=True))


def _error(message: str, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def cmd_accounts_import(args: argparse.Namespace) -> int:
    store = AccountStore()
    try:
        account = store.import_account(args.ref, args.path, weight=args.weight)
    except AccountError as exc:
        return _error(str(exc), as_json=args.json)
    _print(account.to_status_dict(), as_json=args.json, human=f"imported {account.ref}")
    return 0


def cmd_accounts_list(args: argparse.Namespace) -> int:
    store = AccountStore()
    accounts = [a.to_status_dict() for a in store.list()]
    if args.json:
        print(json.dumps({"accounts": accounts}, indent=2, sort_keys=True))
    else:
        if not accounts:
            print("(no accounts imported)")
        for a in accounts:
            print(f"{a['ref']:<20} enabled={a['enabled']!s:<5} weight={a['weight']:<3} auth_present={a['auth_present']!s:<5} quota={a['quota']}")
    return 0


def _print_login_event_human(event: dict) -> None:
    if event["event"] == "authorization_required":
        print(f"Open {event['verification_uri']} and enter code: {event['user_code']}")
        print(f"Waiting for approval (up to {event['expires_in']}s)...")
    elif event["event"] == "completed":
        a = event["account"]
        print(f"logged in {a['ref']} enabled={a['enabled']} weight={a['weight']}")


def cmd_accounts_login(args: argparse.Namespace) -> int:
    if not args.device:
        return _error(
            "only device-code login (--device) is implemented by this CLI frontend; "
            "browser OAuth login is not supported here",
            as_json=args.json,
        )

    from .device_login import DeviceLoginError, run_device_login

    try:
        with httpx.Client() as client:
            for event in run_device_login(args.ref, client=client, weight=args.weight):
                if args.json:
                    print(json.dumps(event, sort_keys=True))
                    sys.stdout.flush()
                else:
                    _print_login_event_human(event)
    except (DeviceLoginError, AccountError) as exc:
        return _error(str(exc), as_json=args.json)
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    from .quota import read_quota

    store = AccountStore()
    accounts = store.list()
    results = []
    for account in accounts:
        quota = read_quota(account.auth_path)
        # Persist only the bounded own-state observation needed by routing:
        # an explicit 100% window excludes an account, while unknown data
        # clears the observation and remains eligible.
        store.set_quota_exhausted(account.ref, quota.exhausted)
        results.append({"ref": account.ref, "quota": quota.to_dict()})
    if args.json:
        print(json.dumps({"accounts": results}, indent=2, sort_keys=True))
    else:
        if not accounts:
            print("(no accounts imported)")
        for r in results:
            q = r["quota"]
            primary = q["primary_used_percent"]
            primary_s = f"{primary}%" if primary is not None else "unknown"
            remaining = q.get("primary_remaining_percent")
            remaining_s = f"{remaining}%" if remaining is not None else "unknown"
            status = q.get("status", "unavailable" if q.get("error") else "ok")
            err = f" error={q['error']}" if q.get("error") else ""
            print(
                f"{r['ref']:<20} primary_remaining={remaining_s} "
                f"primary_used={primary_s} status={status}{err}"
            )
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import run_tui

    run_tui()
    return 0


def cmd_pool_enable(args: argparse.Namespace) -> int:
    return _pool_toggle(args, enabled=True)


def cmd_pool_disable(args: argparse.Namespace) -> int:
    return _pool_toggle(args, enabled=False)


def _pool_toggle(args: argparse.Namespace, *, enabled: bool) -> int:
    store = AccountStore()
    try:
        account = store.set_enabled(args.ref, enabled)
    except AccountError as exc:
        return _error(str(exc), as_json=args.json)
    _print(account.to_status_dict(), as_json=args.json, human=f"{'enabled' if enabled else 'disabled'} {account.ref}")
    return 0


def cmd_pool_weight(args: argparse.Namespace) -> int:
    store = AccountStore()
    try:
        account = store.set_weight(args.ref, args.weight)
    except AccountError as exc:
        return _error(str(exc), as_json=args.json)
    _print(account.to_status_dict(), as_json=args.json, human=f"weight({account.ref})={account.weight}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = AccountStore()
    records = store.list()
    accounts = []
    for account in records:
        d = account.to_status_dict()
        d["authenticated"] = CodexTokenManager(account.auth_path).is_authenticated() if d["auth_present"] else False
        accounts.append(d)
    status = {
        "accounts": accounts,
        "eligible_count": sum(
            1
            for account, status_account in zip(records, accounts)
            if status_account["enabled"]
            and status_account["authenticated"]
            and account.quota_exhausted is not True
        ),
    }
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(f"Codex Pool — {len(accounts)} account(s), {status['eligible_count']} eligible")
        for a in accounts:
            print(f"  {a['ref']:<20} enabled={a['enabled']!s:<5} weight={a['weight']:<3} authenticated={a['authenticated']!s:<5} quota={a['quota']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    from .chain import DEFAULT_MAX_SESSIONS, ChainStore
    from .server import create_app
    from .upstream import CodexHTTPUpstream

    api_key = args.api_key
    if not api_key:
        return _error("--api-key is required (or set CODEX_POOL_API_KEY)", as_json=args.json)

    host, _, port_s = args.listen.rpartition(":")
    host = host or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        return _error(
            f"--listen host must be loopback ({'/'.join(_LOOPBACK_HOSTS)}); got {host!r} — remote listening is not supported",
            as_json=args.json,
        )
    try:
        port = int(port_s)
    except ValueError:
        return _error(f"invalid --listen value: {args.listen!r} (expected host:port)", as_json=args.json)
    if not (1 <= port <= 65535):
        return _error(f"invalid --listen port: {port} (must be 1-65535)", as_json=args.json)

    # Read once at startup; there is no runtime reload.
    raw_max_sessions = os.environ.get("CODEX_POOL_MAX_SESSIONS")
    if raw_max_sessions is None:
        max_sessions = DEFAULT_MAX_SESSIONS
    else:
        try:
            max_sessions = int(raw_max_sessions)
        except ValueError:
            max_sessions = 0
        if max_sessions < 1:
            return _error(
                f"invalid CODEX_POOL_MAX_SESSIONS value: {raw_max_sessions!r} (must be a positive integer)",
                as_json=args.json,
            )

    app = create_app(
        accounts=AccountStore(),
        chain_store=ChainStore(max_records=max_sessions),
        upstream=CodexHTTPUpstream(),
        api_key=api_key,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(prog="codex-pool", description="Codex account pool + local Responses API proxy")
    sub = parser.add_subparsers(dest="command")

    accounts_p = sub.add_parser("accounts", help="manage imported accounts")
    accounts_sub = accounts_p.add_subparsers(dest="accounts_command", required=True)

    imp = accounts_sub.add_parser("import", help="import an account from a local auth-file fixture/path")
    imp.add_argument("ref", help="short account reference name")
    imp.add_argument("--path", required=True, help="path to the Codex OAuth auth JSON file")
    imp.add_argument("--weight", type=int, default=1)
    imp.add_argument("--json", action="store_true")
    imp.set_defaults(func=cmd_accounts_import)

    lst = accounts_sub.add_parser("list", help="list imported accounts")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_accounts_list)

    login = accounts_sub.add_parser("login", help="log in an account via Codex device-code OAuth")
    login.add_argument("ref", help="short account reference name")
    login.add_argument("--device", action="store_true", help="use device-code login (the only supported mode)")
    login.add_argument("--weight", type=int, default=1, help="pool weight for a newly-created ref (ignored on re-login)")
    login.add_argument("--json", action="store_true")
    login.set_defaults(func=cmd_accounts_login)

    pool_p = sub.add_parser("pool", help="manage pool membership/weight")
    pool_sub = pool_p.add_subparsers(dest="pool_command", required=True)

    en = pool_sub.add_parser("enable", help="enable an account in the pool")
    en.add_argument("ref")
    en.add_argument("--json", action="store_true")
    en.set_defaults(func=cmd_pool_enable)

    dis = pool_sub.add_parser("disable", help="disable an account in the pool")
    dis.add_argument("ref")
    dis.add_argument("--json", action="store_true")
    dis.set_defaults(func=cmd_pool_disable)

    wt = pool_sub.add_parser("weight", help="set an account's pool weight")
    wt.add_argument("ref")
    wt.add_argument("weight", type=int)
    wt.add_argument("--json", action="store_true")
    wt.set_defaults(func=cmd_pool_weight)

    st = sub.add_parser("status", help="show pool + service status")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    srv = sub.add_parser("serve", help="run the local Responses API server")
    srv.add_argument("--listen", default="127.0.0.1:8765")
    srv.add_argument("--api-key", default=None, help="local bearer access key (default: $CODEX_POOL_API_KEY)")
    srv.add_argument("--json", action="store_true")
    srv.set_defaults(func=cmd_serve)

    qp = sub.add_parser("quota", help="show real per-account Codex OAuth rate-limit usage")
    qp.add_argument("--json", action="store_true")
    qp.set_defaults(func=cmd_quota)

    tp = sub.add_parser("tui", help="launch the interactive Textual UI")
    tp.set_defaults(func=cmd_tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    _set_json_hint(parser, "--json" in raw_argv)
    args = parser.parse_args(raw_argv)
    if args.command is None:
        return cmd_tui(args)
    if getattr(args, "api_key", None) is None and args.command == "serve":
        args.api_key = os.environ.get("CODEX_POOL_API_KEY")
    try:
        return args.func(args)
    except AccountError as exc:
        return _error(str(exc), as_json=getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
