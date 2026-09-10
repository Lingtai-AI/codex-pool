"""Real quota reader — one Codex account's OAuth rate-limit window.

Adapted from the LingTai kernel's ``src/lingtai/llm/openai/codex_quota.py``
(Apache License, Version 2.0; see ``NOTICE``): spawns a throwaway ``codex
app-server`` with ``$CODEX_HOME`` pointed at a process-owned mode-0700 temp
dir holding a mode-0600 native Codex CLI auth envelope translated from this
account's auth file (the real auth file is never mutated), then drives the
bare newline-delimited JSON-RPC handshake ``initialize`` -> ``initialized``
-> ``account/rateLimits/read`` over stdio.

**Exact field coverage carried over from the source, not invented:** the
source only ever reads ``result["rateLimits"]["primary"]["usedPercent"]`` —
it does not parse a ``secondary`` window or any reset-timestamp field
anywhere. This module extracts ``primary_used_percent`` the same way, and
extracts ``secondary_used_percent`` using the *identical* defensive
`{"usedPercent": ...}` shape under ``rateLimits["secondary"]`` (Codex's
public rate-limit windows are documented as primary+secondary, and the wire
shape is structurally symmetric) — but this key was never exercised against
a real response in the source repo, so treat it with the same confidence as
"parsed defensively, not source-verified". ``primary_reset_at`` /
``secondary_reset_at`` have **no known field name anywhere in the available
source** and are therefore always ``None`` in this pass — a real gap, not a
guess (see CLI_CONTRACT.md).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_CODEX_BIN = "codex"
_APP_SERVER_ARGS = ("app-server",)
_TIMEOUT_SECONDS = 10.0


class _Unavailable(Exception):
    """Internal fail-soft signal; carries a short nonsecret reason code."""


@dataclass
class QuotaResult:
    primary_used_percent: float | None
    secondary_used_percent: float | None
    primary_reset_at: str | None
    secondary_reset_at: str | None
    observed_at: str
    error: str | None = None

    @property
    def exhausted(self) -> bool | None:
        """Whether this observation proves an account is currently exhausted.

        A 100% primary or secondary window is the only explicit exhaustion
        signal this reader knows. If all windows are unknown, preserve unknown
        rather than treating it as zero or exhausted.
        """
        values = [value for value in (self.primary_used_percent, self.secondary_used_percent) if value is not None]
        if any(value >= 100.0 for value in values):
            return True
        if values:
            return False
        return None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "primary_used_percent": self.primary_used_percent,
            "secondary_used_percent": self.secondary_used_percent,
            "primary_reset_at": self.primary_reset_at,
            "secondary_reset_at": self.secondary_reset_at,
            "observed_at": self.observed_at,
        }
        if self.error is not None:
            d["error"] = self.error
        return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unknown(observed_at: str, error: str) -> QuotaResult:
    return QuotaResult(None, None, None, None, observed_at, error=error)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (IndexError, ValueError, binascii.Error, UnicodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _native_codex_auth_payload(auth_path: Path) -> dict[str, Any]:
    try:
        source = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _Unavailable(f"auth_read_failed:{type(exc).__name__}") from exc
    if not isinstance(source, dict):
        raise _Unavailable("auth_malformed")

    access_token = source.get("access_token")
    refresh_token = source.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise _Unavailable("auth_access_token_missing")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _Unavailable("auth_refresh_token_missing")

    access_claims = _decode_jwt_payload(access_token)
    openai_auth = access_claims.get("https://api.openai.com/auth")
    claimed_account_id = openai_auth.get("chatgpt_account_id") if isinstance(openai_auth, dict) else None
    account_id = source.get("chatgpt_account_id") or source.get("account_id") or claimed_account_id
    if not isinstance(account_id, str) or not account_id:
        raise _Unavailable("auth_account_id_missing")

    id_token = source.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        id_token = access_token

    issued_at = access_claims.get("iat")
    try:
        last_refresh = datetime.fromtimestamp(float(issued_at), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        last_refresh = _now_iso()

    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": last_refresh,
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
            "id_token": id_token,
        },
    }


def _prepare_temp_codex_home(auth_path: Path) -> tuple[Path, tempfile.TemporaryDirectory]:
    tmpdir = tempfile.TemporaryDirectory(prefix="codex-pool-quota-")
    try:
        home = Path(tmpdir.name)
        os.chmod(home, stat.S_IRWXU)
        dest = home / "auth.json"
        native_auth = _native_codex_auth_payload(auth_path)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(native_auth, handle, separators=(",", ":"))
        return home, tmpdir
    except _Unavailable:
        tmpdir.cleanup()
        raise
    except (OSError, TypeError, ValueError) as exc:
        tmpdir.cleanup()
        raise _Unavailable(f"auth_materialization_failed:{type(exc).__name__}") from exc


def _stdout_reader_thread(proc: subprocess.Popen, line_queue: "queue.Queue[str | None]") -> None:
    def _run() -> None:
        try:
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ""):
                    line_queue.put(line)
        except (ValueError, OSError):
            pass
        finally:
            line_queue.put(None)

    threading.Thread(target=_run, daemon=True).start()


def _read_line_until(line_queue: "queue.Queue[str | None]", predicate, deadline: float) -> dict[str, Any] | None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = line_queue.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and predicate(obj):
            return obj


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
    except Exception:  # noqa: BLE001 - process cleanup must never raise
        pass
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


PopenFactory = Callable[..., subprocess.Popen]


def _run_app_server_read(codex_home: Path, timeout_seconds: float, *, popen: PopenFactory) -> dict[str, Any]:
    if shutil.which(_CODEX_BIN) is None:
        raise _Unavailable("codex_binary_not_found")

    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    env["CODEX_DISABLE_ANALYTICS"] = "1"

    try:
        proc = popen(
            [_CODEX_BIN, *_APP_SERVER_ARGS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise _Unavailable(f"spawn_failed:{type(exc).__name__}") from exc

    deadline = time.monotonic() + timeout_seconds
    line_queue: "queue.Queue[str | None]" = queue.Queue()
    _stdout_reader_thread(proc, line_queue)
    try:
        assert proc.stdin is not None
        proc.stdin.write(
            json.dumps({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-pool", "version": "1.0"}}}) + "\n"
        )
        proc.stdin.flush()

        init_response = _read_line_until(line_queue, lambda o: o.get("id") == 1, deadline)
        if init_response is None:
            raise _Unavailable("initialize_timeout_or_eof")
        if "error" in init_response:
            raise _Unavailable("initialize_error")

        proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        proc.stdin.flush()

        proc.stdin.write(json.dumps({"id": 2, "method": "account/rateLimits/read", "params": None}) + "\n")
        proc.stdin.flush()

        read_response = _read_line_until(line_queue, lambda o: o.get("id") == 2, deadline)
        if read_response is None:
            raise _Unavailable("read_timeout_or_eof")
        if "error" in read_response:
            raise _Unavailable("read_error")
        result = read_response.get("result")
        if not isinstance(result, dict):
            raise _Unavailable("malformed_result")
        return result
    except BrokenPipeError as exc:
        raise _Unavailable(f"broken_pipe:{type(exc).__name__}") from exc
    finally:
        _terminate_process(proc)


def _extract_used_percent(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    used = float(used)
    if not math.isfinite(used) or not 0.0 <= used <= 100.0:
        return None
    return used


def read_quota(
    auth_path: str | Path,
    *,
    popen: PopenFactory = subprocess.Popen,
    timeout_seconds: float = _TIMEOUT_SECONDS,
) -> QuotaResult:
    """Read one account's current Codex OAuth rate-limit usage.

    Never raises: any failure (missing/malformed auth file, ``codex`` binary
    missing, spawn/handshake/timeout failure) is reported as
    ``primary_used_percent=None`` + a short nonsecret ``error`` string — never
    treated as ``0`` and never leaks token/auth-path contents.
    """
    observed_at = _now_iso()
    auth_path = Path(auth_path).expanduser()
    if not auth_path.is_file():
        return _unknown(observed_at, "auth_file_not_found")

    tmpdir_handle: tempfile.TemporaryDirectory | None = None
    try:
        codex_home, tmpdir_handle = _prepare_temp_codex_home(auth_path)
        result = _run_app_server_read(codex_home, timeout_seconds, popen=popen)
    except _Unavailable as exc:
        return _unknown(observed_at, str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-soft contract; never raise
        return _unknown(observed_at, f"unexpected_error:{type(exc).__name__}")
    finally:
        if tmpdir_handle is not None:
            try:
                tmpdir_handle.cleanup()
            except Exception:  # noqa: BLE001
                pass

    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return _unknown(observed_at, "malformed_result")
    primary_used = _extract_used_percent(rate_limits.get("primary"))
    secondary_used = _extract_used_percent(rate_limits.get("secondary"))
    return QuotaResult(primary_used, secondary_used, None, None, observed_at)
