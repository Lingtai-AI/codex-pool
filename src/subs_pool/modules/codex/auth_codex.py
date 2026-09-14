"""Codex OAuth token manager and structural error classifiers.

Adapted from LingTai kernel ``src/lingtai/auth/codex.py`` (Apache License,
Version 2.0; see ``LICENSES/lingtai-kernel-Apache-2.0.txt`` and ``NOTICE`` in
this repository). Trimmed to what a standalone account/pool owner needs:
token read/refresh and the two structural error classifiers used for
account-eligibility decisions. Session/molt/pool-selection logic from the
original kernel is intentionally not carried over — it is not part of this
project's contract (see ANATOMY.md / CONTRACT.md).
"""

from __future__ import annotations

import base64
import binascii
import asyncio
import json
import os
import threading
import time
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_BUFFER_SECONDS = 300  # refresh if within 5 minutes of expiry

_OAUTH_AUTH_CLAIM = "https://api.openai.com/auth"
_ACCOUNT_ID_CLAIM = "chatgpt_account_id"

_USAGE_LIMIT_CODE = "usage_limit_reached"
_TOKEN_EXPIRED_CODE = "token_expired"


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload locally (NO signature verification)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else {}
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}


class CodexAuthError(Exception):
    """Raised when Codex OAuth tokens cannot be refreshed. Message is user-facing."""


class CodexAuthFormatError(CodexAuthError):
    """Raised for a syntactically valid but structurally invalid auth file."""


class CodexRequestTimeout(CodexAuthError):
    """A shared Codex request reached its absolute deadline."""


class CodexRequestCancelled(CodexRequestTimeout):
    """A shared Codex request observed cooperative cancellation."""


_STRING_AUTH_FIELDS = (
    "access_token",
    "refresh_token",
    "id_token",
    "account_id",
    "chatgpt_account_id",
)


def _validated_auth_data(data: object) -> dict:
    """Validate auth-file JSON at the file boundary without exposing secrets."""
    if not isinstance(data, dict):
        raise CodexAuthFormatError("invalid Codex auth file")
    for key in _STRING_AUTH_FIELDS:
        if key in data and not isinstance(data[key], str):
            raise CodexAuthFormatError("invalid Codex auth file")
    if "expires_at" in data and (
        isinstance(data["expires_at"], bool) or not isinstance(data["expires_at"], (int, float))
    ):
        raise CodexAuthFormatError("invalid Codex auth file")
    return data


async def _request_json_async(
    method: str,
    url: str,
    *,
    transport: httpx.BaseTransport | None,
    absolute_deadline: float,
    monotonic_fn,
    cancel_event: threading.Event | None,
    headers: dict[str, str] | None,
    data: dict[str, str] | None,
) -> httpx.Response:
    """Own one cancellable HTTPX request and its response lifetime."""

    def remaining() -> float:
        if cancel_event is not None and cancel_event.is_set():
            raise CodexRequestCancelled("Codex request was cancelled.")
        value = absolute_deadline - monotonic_fn()
        if value <= 0:
            raise CodexRequestTimeout("Codex request exceeded its deadline.")
        return value

    async def receive() -> httpx.Response:
        timeout = httpx.Timeout(remaining())
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with client.stream(method, url, headers=headers, data=data) as live:
                chunks: list[bytes] = []
                async for chunk in live.aiter_bytes():
                    remaining()
                    chunks.append(chunk)
                remaining()
                decoded_headers = [
                    (name, value)
                    for name, value in live.headers.multi_items()
                    if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
                ]
                return httpx.Response(
                    live.status_code,
                    headers=decoded_headers,
                    content=b"".join(chunks),
                    request=live.request,
                )

    async def cancel_or_deadline() -> None:
        # A threading.Event cannot be awaited directly. This watcher is paired
        # with task cancellation, so a blocked async header/body read is
        # actively unwound instead of merely being checked after it returns.
        while True:
            remaining()
            await asyncio.sleep(min(0.01, remaining()))

    receive_task = asyncio.create_task(receive())
    control_task = asyncio.create_task(cancel_or_deadline())
    done, _ = await asyncio.wait(
        (receive_task, control_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if receive_task in done:
        control_task.cancel()
        await asyncio.gather(control_task, return_exceptions=True)
        return receive_task.result()

    reason = control_task.exception()
    receive_task.cancel()
    await asyncio.gather(receive_task, return_exceptions=True)
    if isinstance(reason, (CodexRequestCancelled, CodexRequestTimeout)):
        raise reason
    raise CodexRequestTimeout("Codex request exceeded its deadline.")


def request_json(
    method: str,
    url: str,
    *,
    transport: httpx.BaseTransport | None,
    timeout_seconds: float,
    deadline: float | None = None,
    monotonic_fn=time.monotonic,
    cancel_event: threading.Event | None = None,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    """Make one owned Codex request under one absolute budget.

    The sync API runs an owned async task in the calling quota worker. A
    deadline/cancellation watcher cancels that task, which unwinds HTTPX's
    header/body operation and its client/response context managers. No helper
    thread, retry, or abandoned request is used.
    """

    # A direct caller that does not supply a shared deadline still gets one
    # absolute request budget. HTTPX's timeout remains an inactivity guard,
    # while this deadline governs the complete streamed body.
    absolute_deadline = deadline if deadline is not None else monotonic_fn() + timeout_seconds
    try:
        response = asyncio.run(
            _request_json_async(
                method,
                url,
                transport=transport,
                absolute_deadline=absolute_deadline,
                monotonic_fn=monotonic_fn,
                cancel_event=cancel_event,
                headers=headers,
                data=data,
            )
        )
    except (CodexRequestTimeout, CodexRequestCancelled):
        raise
    except httpx.TimeoutException:
        raise CodexRequestTimeout("Codex request timed out.") from None
    return response


class CodexTokenManager:
    """Manages a single Codex OAuth token file identified by an explicit path.

    Unlike the original kernel version there is no ``LINGTAI_TUI_DIR``
    fallback: a Codex module account's auth path always comes from the account
    record created by ``subspool codex account import``.
    """

    def __init__(
        self,
        token_path: str,
        *,
        transport: httpx.BaseTransport | None = None,
        time_fn=time.time,
        monotonic_fn=time.monotonic,
        refresh_timeout: float = 30.0,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if not token_path:
            raise ValueError("token_path is required")
        self._path = Path(token_path).expanduser()
        self._lock_path = self._path.with_suffix(".json.lock")
        self._cache: dict | None = None
        self._cache_mtime: float = 0.0
        self._transport = transport
        self._time = time_fn
        self._monotonic = monotonic_fn
        self._refresh_timeout = refresh_timeout
        self._deadline = deadline if deadline is not None else monotonic_fn() + refresh_timeout
        self._cancel_event = cancel_event

    def _remaining(self) -> float:
        return max(0.0, self._deadline - self._monotonic())

    def _check_work(self) -> float:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise CodexRequestCancelled("Codex token refresh was cancelled.")
        remaining = self._remaining()
        if remaining <= 0:
            raise CodexRequestTimeout("Codex token refresh exceeded its deadline.")
        return remaining

    def _acquire_lock(self) -> FileLock:
        """Acquire the auth lock in bounded, cancellation-aware slices."""
        while True:
            remaining = self._check_work()
            lock = FileLock(str(self._lock_path))
            try:
                lock.acquire(timeout=min(0.1, remaining))
            except Timeout:
                continue
            try:
                self._check_work()
            except BaseException:
                lock.release()
                raise
            return lock

    def is_authenticated(self) -> bool:
        try:
            data = self._read()
            return isinstance(data.get("refresh_token"), str) and bool(data["refresh_token"])
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, CodexAuthFormatError):
            return False

    def get_access_token(self) -> str:
        data = self._read()
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool) or self._time() + REFRESH_BUFFER_SECONDS >= expires_at:
            self._refresh(data)
            data = self._read()
        return data["access_token"]

    def refresh_access_token(self, rejected_access_token: str) -> str:
        if not isinstance(rejected_access_token, str) or not rejected_access_token:
            raise ValueError("rejected_access_token must be a non-empty string")
        data = self._read()
        self._refresh(data, rejected_access_token=rejected_access_token)
        return self._read()["access_token"]

    def get_account_id(self) -> str | None:
        try:
            data = self._read()
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, CodexAuthFormatError):
            return None
        return self._extract_account_id(data)

    @staticmethod
    def _extract_account_id(data: dict) -> str | None:
        for key in ("account_id", "chatgpt_account_id"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
        id_token = data.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            return None
        payload = _decode_jwt_payload(id_token)
        auth_claim = payload.get(_OAUTH_AUTH_CLAIM)
        if isinstance(auth_claim, dict):
            val = auth_claim.get(_ACCOUNT_ID_CLAIM)
            if isinstance(val, str) and val:
                return val
        return None

    def _read(self) -> dict:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            raise FileNotFoundError(f"Codex auth file not found: {self._path}")
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        with open(self._path, "r", encoding="utf-8") as f:
            data = _validated_auth_data(json.load(f))
        self._cache = data
        self._cache_mtime = mtime
        return data

    def _refresh(self, data: dict, *, rejected_access_token: str | None = None) -> None:
        self._check_work()
        lock = self._acquire_lock()
        try:
            self._cache = None
            self._cache_mtime = 0.0
            fresh = self._read()
            expires_safely = fresh.get("expires_at", 0) > self._time() + REFRESH_BUFFER_SECONDS
            already_replaced = bool(
                rejected_access_token and fresh.get("access_token") != rejected_access_token
            )
            if expires_safely and (rejected_access_token is None or already_replaced):
                return

            remaining = self._check_work()

            refresh_token = fresh.get("refresh_token") or data.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("No refresh_token available in auth file.")

            request_data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            }
            response = request_json(
                "POST",
                TOKEN_URL,
                transport=self._transport,
                timeout_seconds=min(self._refresh_timeout, remaining),
                deadline=self._deadline,
                monotonic_fn=self._monotonic,
                cancel_event=self._cancel_event,
                data=request_data,
            )
            self._check_work()
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise CodexAuthError(
                        "Codex session expired. Re-run `subspool codex account login` to re-authenticate."
                    ) from e
                raise
            result = response.json()

            fresh["access_token"] = result["access_token"]
            if "refresh_token" in result:
                fresh["refresh_token"] = result["refresh_token"]
            fresh["expires_at"] = result.get(
                "expires_at", int(self._time()) + result.get("expires_in", 3600)
            )

            tmp_path = self._path.with_suffix(".json.tmp")
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(fresh, f, indent=2)
            tmp_path.replace(self._path)

            self._cache = None
            self._cache_mtime = 0.0
        finally:
            lock.release()


def _structured_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _structured_error_codes(exc: BaseException) -> tuple[str, ...]:
    candidates: list[object] = []
    code_attr = getattr(exc, "code", None)
    if isinstance(code_attr, str):
        candidates.append(code_attr)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            candidates.append(err.get("code"))
            candidates.append(err.get("type"))
        candidates.append(body.get("code"))
    return tuple(c for c in candidates if isinstance(c, str) and c)


def is_token_expired_error(exc: BaseException) -> bool:
    """True iff ``exc`` is structurally HTTP 401 with machine code ``token_expired``."""
    if _structured_status_code(exc) != 401:
        return False
    return _TOKEN_EXPIRED_CODE in _structured_error_codes(exc)


def is_usage_limit_reached_error(exc: BaseException) -> bool:
    """True iff ``exc`` is structurally HTTP 429 with machine code ``usage_limit_reached``."""
    if _structured_status_code(exc) != 429:
        return False
    return _USAGE_LIMIT_CODE in _structured_error_codes(exc)
