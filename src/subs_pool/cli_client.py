"""Thin async subprocess wrapper around a subs-pool module CLI contract.

This module owns no account/token/auth state and makes no provider HTTP calls.
Machine-safe operations use the JSON-only ``subspool-cli`` module entrypoint;
the human TUI's device-login stream uses the human dispatcher with a private
JSONL adapter. The subprocess spawn function is injectable so tests can supply
a fake process without starting a real one.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol


class CLIError(Exception):
    """Raised when a subs-pool module CLI exits non-zero or emits bad output."""

    def __init__(self, message: str, *, returncode: int | None = None, data: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.data = data


class Process(Protocol):
    """The minimal subset of :class:`asyncio.subprocess.Process` used here."""

    returncode: int | None
    stdout: Any
    stderr: Any

    async def communicate(self) -> tuple[bytes, bytes]: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Spawner = Callable[[Sequence[str]], Awaitable[Process]]


async def _default_spawn(args: Sequence[str]) -> Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "subs_pool.agent_cli",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _default_human_spawn(args: Sequence[str]) -> Process:
    """Run the human dispatcher for the TUI-only login event stream."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "subs_pool.cli",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


_MAX_RAW_ERROR_CHARS = 2000
_LOGIN_EXIT_TIMEOUT_SECONDS = 5.0
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|password|secret|api[_-]?key|authorization)"
    r"(\s*[:=]\s*)([\"']?)([^\"'\s,}]+)"
)
_SENSITIVE_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,}]+")
_SENSITIVE_SECRET_PREFIX = re.compile(r"\b(?:sk|sess|rt|at)-[A-Za-z0-9_-]{8,}\b")


def _safe_text(text: str) -> str:
    """Bound and redact common credential forms in fallback diagnostics."""
    text = _SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    text = _SENSITIVE_BEARER.sub("Bearer <redacted>", text)
    text = _SENSITIVE_SECRET_PREFIX.sub("<redacted>", text)
    return text if len(text) <= _MAX_RAW_ERROR_CHARS else text[:_MAX_RAW_ERROR_CHARS] + "… (truncated)"


def _parse_error(stderr: bytes) -> str:
    text = stderr.decode(errors="replace").strip()
    if not text:
        return "subs-pool module CLI exited with an error (no message)"
    # Contract errors are one JSON object on the last non-empty stderr line.
    # A broken installation may instead produce a traceback; retain a bounded,
    # redacted diagnostic rather than dumping unbounded/provider-tainted text.
    try:
        payload = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return _safe_text(text)
    if isinstance(payload, dict):
        if isinstance(payload.get("error"), str):
            return _safe_text(payload["error"])
        if isinstance(payload.get("error"), dict):
            return _safe_text(str(payload["error"].get("message", "module CLI failed")))
        return "subs-pool module CLI returned a malformed error response"
    return _safe_text(text)


def _safe_login_event(event: dict) -> dict:
    """Project JSONL events onto the small non-secret frontend contract."""
    kind = event.get("event")
    if kind == "authorization_required":
        return {
            "event": "authorization_required",
            "verification_uri": event.get("verification_uri", ""),
            "user_code": event.get("user_code", ""),
            "expires_in": event.get("expires_in"),
            "interval": event.get("interval"),
        }
    if kind == "completed":
        account = event.get("account")
        if not isinstance(account, dict):
            raise CLIError("malformed login event: completed event lacks account")
        return {
            "event": "completed",
            "account": {
                key: account[key]
                for key in ("ref", "enabled", "weight", "auth_present", "quota")
                if key in account
            },
        }
    # Unknown events are not part of the contract. Preserve only their type so
    # a broken child cannot smuggle arbitrary provider payloads into the UI.
    return {"event": str(kind) if kind is not None else "unknown"}


async def _spawn_or_raise(spawn: Spawner, args: Sequence[str]) -> Process:
    try:
        return await spawn(args)
    except FileNotFoundError as exc:
        raise CLIError(
            "subs-pool CLI not found: is subs-pool installed for this interpreter?"
        ) from exc


async def _terminate_process(proc: Process) -> None:
    """Best-effort terminate and reap a local child process this wrapper owns.

    This only touches our local CLI subprocess; it never claims to cancel a
    request already submitted upstream to a provider.
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            return
        await proc.wait()


class LoginStream:
    """One in-flight device-login event stream for the human frontend.

    Async-iterate this to receive parsed JSONL events as they arrive. A
    ``completed`` line is held until the child has drained its pipes and exited
    with code zero; a line alone is never treated as success. :meth:`cancel`
    stops only the local CLI subprocess.
    """

    def __init__(self, spawn: Spawner, args: Sequence[str]) -> None:
        self._spawn = spawn
        self._args = list(args)
        self._proc: Process | None = None
        self._done = False
        self._cancel_requested = False

    def __aiter__(self) -> "LoginStream":
        return self

    async def __anext__(self) -> dict:
        if self._done:
            raise StopAsyncIteration
        if self._proc is None:
            proc = await _spawn_or_raise(self._spawn, self._args)
            self._proc = proc
            if self._cancel_requested:
                await _terminate_process(proc)
                raise StopAsyncIteration
        assert self._proc.stdout is not None
        raw = await self._proc.stdout.readline()
        if not raw:
            try:
                returncode = await asyncio.wait_for(self._proc.wait(), timeout=_LOGIN_EXIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await _terminate_process(self._proc)
                self._done = True
                raise CLIError("login CLI did not exit after closing stdout")
            self._done = True
            if returncode != 0:
                stderr = await self._proc.stderr.read() if self._proc.stderr is not None else b""
                raise CLIError(_parse_error(stderr), returncode=returncode)
            raise StopAsyncIteration

        line = raw.decode(errors="replace").strip()
        if not line:
            return await self.__anext__()
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            self._done = True
            await _terminate_process(self._proc)
            raise CLIError("malformed login event: could not parse JSON") from exc
        if not isinstance(event, dict):
            self._done = True
            await _terminate_process(self._proc)
            raise CLIError(
                f"malformed login event: expected a JSON object, got {type(event).__name__}"
            )
        if event.get("event") == "completed":
            try:
                safe_event = _safe_login_event(event)
            except CLIError:
                self._done = True
                await _terminate_process(self._proc)
                raise
            return await self._finish_completed(safe_event)
        return _safe_login_event(event)

    async def _finish_completed(self, event: dict) -> dict:
        """Trust ``completed`` only after clean child termination.

        ``communicate`` drains both pipes while waiting. The bounded wait also
        handles a provider-poll process that emits a completion line and then
        hangs, without leaving a child behind or claiming successful login.
        """
        assert self._proc is not None
        try:
            _stdout, stderr = await asyncio.wait_for(
                self._proc.communicate(), timeout=_LOGIN_EXIT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            await _terminate_process(self._proc)
            self._done = True
            raise CLIError("login CLI did not exit cleanly after completion") from exc
        except asyncio.CancelledError:
            await _terminate_process(self._proc)
            self._done = True
            raise
        self._done = True
        returncode = self._proc.returncode
        if returncode is None:
            returncode = await self._proc.wait()
        if returncode != 0:
            raise CLIError(_parse_error(stderr), returncode=returncode)
        return event

    async def cancel(self) -> None:
        """Terminate the in-flight login subprocess, if any (idempotent)."""
        if self._done:
            return
        self._done = True
        proc = self._proc
        if proc is None:
            self._cancel_requested = True
            return
        await _terminate_process(proc)


class CLIClient:
    """Async wrapper for one module's machine command surface."""

    def __init__(
        self,
        module_id: str,
        spawn: Spawner | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.module_id = module_id
        self._spawn = spawn or _default_spawn
        # Bounds one-shot commands (status/quota/pool/import/etc.), not the
        # device-login provider poll window owned by the CLI subprocess.
        self._timeout = timeout

    async def _run_json(self, args: Sequence[str]) -> dict:
        proc = await _spawn_or_raise(
            self._spawn, [self.module_id, *args]
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            await _terminate_process(proc)
            raise CLIError(f"subs-pool module CLI timed out after {self._timeout:g}s") from exc
        except asyncio.CancelledError:
            await _terminate_process(proc)
            raise
        if proc.returncode != 0:
            try:
                payload = json.loads(stdout.decode(errors="replace"))
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    raise CLIError(str(error.get("message", "module CLI failed")), returncode=proc.returncode, data=payload.get("data") if isinstance(payload.get("data"), dict) else None)
            except json.JSONDecodeError:
                pass
            raise CLIError(_parse_error(stderr), returncode=proc.returncode)
        try:
            payload = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise CLIError(f"malformed CLI output: could not parse JSON ({len(stdout)} bytes)") from exc
        if not isinstance(payload, dict):
            raise CLIError(
                f"malformed CLI output: expected a JSON object, got {type(payload).__name__}"
            )
        if payload.get("schema_version") == 1 and "ok" in payload:
            if not payload.get("ok"):
                if isinstance(payload.get("data"), dict):
                    return payload["data"]
                error = payload.get("error")
                message = error.get("message", "module CLI failed") if isinstance(error, dict) else "module CLI failed"
                raise CLIError(str(message), returncode=proc.returncode)
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        return payload

    async def accounts_list(self) -> dict:
        return await self._run_json(["account", "list"])

    async def accounts_import(self, ref: str, path: str, *, weight: int = 1) -> dict:
        return await self._run_json(
            ["account", "import", ref, "--path", path, "--weight", str(weight)]
        )

    async def pool_enable(self, ref: str) -> dict:
        return await self._run_json(["account", "enable", ref])

    async def pool_disable(self, ref: str) -> dict:
        return await self._run_json(["account", "disable", ref])

    async def pool_weight(self, ref: str, weight: int) -> dict:
        return await self._run_json(["account", "weight", ref, str(weight)])

    async def status(self) -> dict:
        return await self._run_json(["status"])

    async def quota(self) -> dict:
        return await self._run_json(["quota"])

    def login(self, ref: str) -> LoginStream:
        """Start login lazily, on first iteration."""
        spawn = _default_human_spawn if self._spawn is _default_spawn else self._spawn
        return LoginStream(
            spawn,
            [self.module_id, "account", "login", ref, "--device", "--events-jsonl"],
        )


__all__ = ["CLIClient", "CLIError", "LoginStream", "Process", "Spawner"]
