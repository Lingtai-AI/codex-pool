from __future__ import annotations

import json

from fakes import write_auth_fixture

from codex_pool.quota import read_quota


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, s: str) -> None:
        self.written.append(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._idx = 0

    def readline(self) -> str:
        if self._idx >= len(self._lines):
            return ""
        line = self._lines[self._idx]
        self._idx += 1
        return line

    def close(self) -> None:
        pass


class FakeProcess:
    """Stands in for ``subprocess.Popen`` — a ``codex app-server`` that replies with ``lines``."""

    def __init__(self, lines: list[str]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = None
        self._terminated = False

    def poll(self):
        return 0 if self._terminated else None

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._terminated = True

    def wait(self, timeout=None) -> int:
        return 0


def _fake_popen_factory(lines: list[str]):
    def _popen(*args, **kwargs):
        return FakeProcess(lines)

    return _popen


_INIT_OK = json.dumps({"id": 1, "result": {}})


def test_quota_success_reads_primary_and_secondary(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_pool.quota.shutil.which", lambda name: "/usr/bin/codex")
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)

    rate_limits_line = json.dumps(
        {
            "id": 2,
            "result": {"rateLimits": {"primary": {"usedPercent": 30}, "secondary": {"usedPercent": 10}}},
        }
    )
    popen = _fake_popen_factory([_INIT_OK + "\n", rate_limits_line + "\n"])

    result = read_quota(str(auth), popen=popen)

    assert result.primary_used_percent == 30
    assert result.secondary_used_percent == 10
    assert result.primary_reset_at is None
    assert result.secondary_reset_at is None
    assert result.error is None
    assert result.observed_at  # non-empty ISO timestamp

    dumped = json.dumps(result.to_dict())
    assert "access_token" not in dumped
    assert "rt-1" not in dumped
    assert "error" not in result.to_dict()


def test_quota_missing_auth_file_is_unknown_not_zero(tmp_path):
    missing = tmp_path / "nope.json"
    result = read_quota(str(missing))
    assert result.primary_used_percent is None
    assert result.error == "auth_file_not_found"


def test_quota_malformed_auth_file_reports_nonsecret_error(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "at-1"}))  # no refresh_token
    result = read_quota(str(auth))
    assert result.primary_used_percent is None
    assert result.error == "auth_refresh_token_missing"


def test_quota_codex_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_pool.quota.shutil.which", lambda name: None)
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    result = read_quota(str(auth))
    assert result.primary_used_percent is None
    assert result.error == "codex_binary_not_found"


def test_quota_handshake_timeout_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_pool.quota.shutil.which", lambda name: "/usr/bin/codex")
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    popen = _fake_popen_factory([])  # server never responds
    result = read_quota(str(auth), popen=popen, timeout_seconds=0.05)
    assert result.primary_used_percent is None
    assert result.error == "initialize_timeout_or_eof"


def test_quota_malformed_rate_limits_shape_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_pool.quota.shutil.which", lambda name: "/usr/bin/codex")
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    bad_result_line = json.dumps({"id": 2, "result": {}})  # no "rateLimits" key at all
    popen = _fake_popen_factory([_INIT_OK + "\n", bad_result_line + "\n"])
    result = read_quota(str(auth), popen=popen)
    assert result.primary_used_percent is None
    assert result.error == "malformed_result"


def test_quota_never_leaks_raw_auth_path_or_tokens_in_error(tmp_path):
    auth = tmp_path / "secret-name-account.json"
    auth.write_text("not json")
    result = read_quota(str(auth))
    assert "secret-name-account" not in (result.error or "")


def test_quota_exhaustion_property_is_explicit_and_unknown_is_not_exhausted():
    from codex_pool.quota import QuotaResult

    assert QuotaResult(100.0, None, None, None, "now").exhausted is True
    assert QuotaResult(99.9, None, None, None, "now").exhausted is False
    assert QuotaResult(None, None, None, None, "now", error="read_failed").exhausted is None
