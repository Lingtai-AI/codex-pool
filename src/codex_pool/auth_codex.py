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
import json
import os
import time
from pathlib import Path

import httpx
from filelock import FileLock

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


class CodexTokenManager:
    """Manages a single Codex OAuth token file identified by an explicit path.

    Unlike the original kernel version there is no ``LINGTAI_TUI_DIR``
    fallback: a codex-pool account's auth path always comes from the account
    record created by ``codex-pool accounts import``.
    """

    def __init__(self, token_path: str) -> None:
        if not token_path:
            raise ValueError("token_path is required")
        self._path = Path(token_path).expanduser()
        self._lock_path = self._path.with_suffix(".json.lock")
        self._cache: dict | None = None
        self._cache_mtime: float = 0.0

    def is_authenticated(self) -> bool:
        try:
            data = self._read()
            return isinstance(data.get("refresh_token"), str) and bool(data["refresh_token"])
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, CodexAuthFormatError):
            return False

    def get_access_token(self) -> str:
        data = self._read()
        expires_at = data.get("expires_at", 0)
        if time.time() + REFRESH_BUFFER_SECONDS >= expires_at:
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
        lock = FileLock(str(self._lock_path), timeout=30)
        with lock:
            self._cache = None
            self._cache_mtime = 0.0
            fresh = self._read()
            expires_safely = fresh.get("expires_at", 0) > time.time() + REFRESH_BUFFER_SECONDS
            already_replaced = bool(
                rejected_access_token and fresh.get("access_token") != rejected_access_token
            )
            if expires_safely and (rejected_access_token is None or already_replaced):
                return

            refresh_token = fresh.get("refresh_token") or data.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("No refresh_token available in auth file.")

            response = httpx.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
                timeout=30,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise CodexAuthError(
                        "Codex session expired. Re-run `codex-pool accounts login` to re-authenticate."
                    ) from e
                raise
            result = response.json()

            fresh["access_token"] = result["access_token"]
            if "refresh_token" in result:
                fresh["refresh_token"] = result["refresh_token"]
            fresh["expires_at"] = result.get(
                "expires_at", int(time.time()) + result.get("expires_in", 3600)
            )

            tmp_path = self._path.with_suffix(".json.tmp")
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(fresh, f, indent=2)
            tmp_path.replace(self._path)

            self._cache = None
            self._cache_mtime = 0.0


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
