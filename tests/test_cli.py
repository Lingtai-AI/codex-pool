from __future__ import annotations

import json
import sys
import types

import pytest

from codex_pool import cli
from codex_pool.accounts import AccountStore
from fakes import write_auth_fixture


def test_accounts_list_json_empty(capsys):
    rc = cli.main(["accounts", "list", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"accounts": []}


def test_argparse_error_honors_json_on_stderr(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["bogus-command", "--json"])
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    payload = json.loads(err)
    assert "error" in payload
    assert isinstance(payload["error"], str)


def test_argparse_error_human_mode_is_not_json(capsys):
    with pytest.raises(SystemExit):
        cli.main(["bogus-command"])
    err = capsys.readouterr().err
    with pytest.raises(json.JSONDecodeError):
        json.loads(err)


def test_login_without_device_flag_errors(capsys):
    rc = cli.main(["accounts", "login", "work", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "device" in payload["error"]


def test_login_device_streams_jsonl_and_registers_account(capsys, monkeypatch):
    def fake_run_device_login(ref, *, client, account_store=None, weight=1, **kwargs):
        yield {
            "event": "authorization_required",
            "verification_uri": "https://auth.openai.com/codex/device",
            "user_code": "ABCD-1234",
            "expires_in": 900,
            "interval": 5,
        }
        yield {
            "event": "completed",
            "account": {"ref": ref, "enabled": True, "weight": weight, "auth_present": True, "quota": "unknown"},
        }

    monkeypatch.setattr("codex_pool.device_login.run_device_login", fake_run_device_login)

    rc = cli.main(["accounts", "login", "work", "--device", "--json"])
    assert rc == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["event"] == "authorization_required"
    assert second["event"] == "completed"
    assert second["account"]["ref"] == "work"


def test_login_device_failure_is_nonzero_json_stderr(capsys, monkeypatch):
    from codex_pool.device_login import DeviceLoginError

    def fake_run_device_login(ref, *, client, account_store=None, weight=1, **kwargs):
        yield {
            "event": "authorization_required",
            "verification_uri": "https://auth.openai.com/codex/device",
            "user_code": "ABCD-1234",
            "expires_in": 900,
            "interval": 5,
        }
        raise DeviceLoginError("device authorization was denied (status 400)")

    monkeypatch.setattr("codex_pool.device_login.run_device_login", fake_run_device_login)

    rc = cli.main(["accounts", "login", "work", "--device", "--json"])
    assert rc == 1
    captured = capsys.readouterr()
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1  # the authorization_required line already flushed
    payload = json.loads(captured.err)
    assert payload["error"] == "device authorization was denied (status 400)"


def test_quota_json_reports_per_account(capsys, monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    AccountStore().import_account("work", str(auth))

    from codex_pool.quota import QuotaResult

    def fake_read_quota(auth_path, **kwargs):
        return QuotaResult(42.0, None, None, None, "2026-01-01T00:00:00+00:00")

    monkeypatch.setattr("codex_pool.quota.read_quota", fake_read_quota)

    rc = cli.main(["quota", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accounts"] == [
        {
            "ref": "work",
            "quota": {
                "primary_used_percent": 42.0,
                "secondary_used_percent": None,
                "primary_reset_at": None,
                "secondary_reset_at": None,
                "observed_at": "2026-01-01T00:00:00+00:00",
            },
        }
    ]


def test_quota_json_persists_known_exhaustion_for_routing(capsys, monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("work", str(auth))

    from codex_pool.quota import QuotaResult

    monkeypatch.setattr(
        "codex_pool.quota.read_quota",
        lambda auth_path, **kwargs: QuotaResult(100.0, None, None, None, "2026-01-01T00:00:00+00:00"),
    )
    assert cli.main(["quota", "--json"]) == 0
    capsys.readouterr()
    assert AccountStore().get("work").quota_exhausted is True


def test_serve_rejects_nonloopback_host(capsys):
    rc = cli.main(["serve", "--listen", "0.0.0.0:9000", "--api-key", "x", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "loopback" in payload["error"]


def test_serve_rejects_invalid_port(capsys):
    rc = cli.main(["serve", "--listen", "127.0.0.1:99999", "--api-key", "x", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "port" in payload["error"]


def test_serve_requires_api_key(capsys, monkeypatch):
    monkeypatch.delenv("CODEX_POOL_API_KEY", raising=False)
    rc = cli.main(["serve", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "api-key" in payload["error"] or "api_key" in payload["error"]


def _capture_serve(monkeypatch):
    """Record what `serve` would construct/run without binding a socket."""
    created: dict = {}
    runs: list = []

    def fake_create_app(**kwargs):
        created.update(kwargs)
        return object()

    monkeypatch.setattr("codex_pool.server.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: runs.append(kwargs))
    return created, runs


def test_serve_max_sessions_defaults_to_hundred_thousand(monkeypatch):
    monkeypatch.delenv("CODEX_POOL_MAX_SESSIONS", raising=False)
    created, runs = _capture_serve(monkeypatch)
    assert cli.main(["serve", "--api-key", "x", "--json"]) == 0
    assert created["chain_store"]._max_records == 100000
    assert len(runs) == 1


def test_serve_reads_max_sessions_from_env_at_startup(monkeypatch):
    monkeypatch.setenv("CODEX_POOL_MAX_SESSIONS", "3")
    created, runs = _capture_serve(monkeypatch)
    assert cli.main(["serve", "--api-key", "x", "--json"]) == 0
    assert created["chain_store"]._max_records == 3
    assert len(runs) == 1


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5", ""])
def test_serve_rejects_invalid_max_sessions_with_json_error(capsys, monkeypatch, raw):
    monkeypatch.setenv("CODEX_POOL_MAX_SESSIONS", raw)
    created, runs = _capture_serve(monkeypatch)
    assert cli.main(["serve", "--api-key", "x", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert "CODEX_POOL_MAX_SESSIONS" in payload["error"]
    assert "positive integer" in payload["error"]
    assert created == {} and runs == []


def test_serve_rejects_invalid_max_sessions_in_human_mode(capsys, monkeypatch):
    monkeypatch.setenv("CODEX_POOL_MAX_SESSIONS", "0")
    created, runs = _capture_serve(monkeypatch)
    assert cli.main(["serve", "--api-key", "x"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "CODEX_POOL_MAX_SESSIONS" in err
    assert created == {} and runs == []


def test_status_reports_malformed_auth_as_unavailable(capsys, tmp_path):
    auth = tmp_path / "malformed.json"
    auth.write_text("null", encoding="utf-8")
    AccountStore().import_account("broken", str(auth))
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eligible_count"] == 0
    assert payload["accounts"][0]["authenticated"] is False


def test_no_args_dispatches_to_tui(monkeypatch):
    calls = []
    fake_tui = types.ModuleType("codex_pool.tui")
    fake_tui.run_tui = lambda: calls.append("ran")
    monkeypatch.setitem(sys.modules, "codex_pool.tui", fake_tui)

    rc = cli.main([])
    assert rc == 0
    assert calls == ["ran"]


def test_tui_subcommand_dispatches(monkeypatch):
    calls = []
    fake_tui = types.ModuleType("codex_pool.tui")
    fake_tui.run_tui = lambda: calls.append("ran")
    monkeypatch.setitem(sys.modules, "codex_pool.tui", fake_tui)

    rc = cli.main(["tui"])
    assert rc == 0
    assert calls == ["ran"]
