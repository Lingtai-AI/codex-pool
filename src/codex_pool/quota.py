"""Read one Codex account's current quota from the read-only WHAM endpoint.

The quota command reads the account's existing flat auth file and makes one
direct ``GET https://chatgpt.com/backend-api/wham/usage`` request. It never
refreshes or writes auth, starts a Codex subprocess, copies tokens to a
temporary home, retries, or falls back to another account or transport.

WHAM returns ``rate_limit`` (or ``rateLimits``) containing primary and
secondary windows. Window percentages, reset timestamps, names, and actual
durations are normalized defensively. Missing or invalid numeric values stay
unknown (``None``); in particular, unknown is never reported as zero.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

_WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_TIMEOUT_SECONDS = 10.0
_OAUTH_AUTH_CLAIM = "https://api.openai.com/auth"
_ACCOUNT_ID_CLAIM = "chatgpt_account_id"


class _Unavailable(Exception):
    """Internal fail-soft signal carrying a short, nonsecret reason code."""


@dataclass
class QuotaResult:
    # These five fields are the original frozen CLI surface. Keep their
    # position and spelling so existing callers and positional test fixtures
    # continue to work.
    primary_used_percent: float | None
    secondary_used_percent: float | None
    primary_reset_at: str | None
    secondary_reset_at: str | None
    observed_at: str
    error: str | None = None

    # Additive WHAM facts. Labels and durations are never guessed.
    status: str = "ok"
    primary_window_name: str | None = None
    secondary_window_name: str | None = None
    primary_window_duration_mins: float | None = None
    secondary_window_duration_mins: float | None = None

    def __post_init__(self) -> None:
        if self.error is not None:
            self.status = "unavailable"

    @property
    def primary_remaining_percent(self) -> float | None:
        return _remaining_percent(self.primary_used_percent)

    @property
    def secondary_remaining_percent(self) -> float | None:
        return _remaining_percent(self.secondary_used_percent)

    @property
    def exhausted(self) -> bool | None:
        """Whether this observation proves an account is currently exhausted.

        A 100% primary or secondary window is the only explicit exhaustion
        signal this reader knows. If all windows are unknown, preserve unknown
        rather than treating it as zero or exhausted.
        """
        values = [
            value
            for value in (self.primary_used_percent, self.secondary_used_percent)
            if value is not None
        ]
        if any(value >= 100.0 for value in values):
            return True
        if values:
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "primary_used_percent": self.primary_used_percent,
            "secondary_used_percent": self.secondary_used_percent,
            "primary_reset_at": self.primary_reset_at,
            "secondary_reset_at": self.secondary_reset_at,
            "observed_at": self.observed_at,
            "status": self.status,
            "primary_remaining_percent": self.primary_remaining_percent,
            "secondary_remaining_percent": self.secondary_remaining_percent,
            "primary_window_name": self.primary_window_name,
            "secondary_window_name": self.secondary_window_name,
            "primary_window_duration_mins": self.primary_window_duration_mins,
            "secondary_window_duration_mins": self.secondary_window_duration_mins,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass(frozen=True)
class _Window:
    used_percent: float | None
    reset_at: str | None
    name: str | None
    duration_mins: float | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unknown(observed_at: str, error: str) -> QuotaResult:
    return QuotaResult(
        None,
        None,
        None,
        None,
        observed_at,
        error=error,
        status="unavailable",
    )


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _account_id_from_claims(token: Any) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    claims = _decode_jwt_payload(token)
    auth_claim = claims.get(_OAUTH_AUTH_CLAIM)
    if not isinstance(auth_claim, Mapping):
        return None
    account_id = auth_claim.get(_ACCOUNT_ID_CLAIM)
    return account_id if isinstance(account_id, str) and account_id else None


def _read_existing_auth(auth_path: Path) -> tuple[str, str | None]:
    """Read the existing token/account facts without refresh or modification."""
    try:
        source = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _Unavailable(f"auth_read_failed:{type(exc).__name__}") from exc
    if not isinstance(source, dict):
        raise _Unavailable("auth_malformed")

    access_token = source.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise _Unavailable("auth_access_token_missing")

    account_id: str | None = None
    for key in ("account_id", "chatgpt_account_id"):
        candidate = source.get(key)
        if isinstance(candidate, str) and candidate:
            account_id = candidate
            break
    if account_id is None:
        account_id = _account_id_from_claims(source.get("id_token"))
    if account_id is None:
        account_id = _account_id_from_claims(access_token)
    return access_token, account_id


def _request_usage(
    access_token: str,
    account_id: str | None,
    *,
    timeout_seconds: float,
    transport: httpx.BaseTransport | None,
) -> Mapping[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if account_id is not None:
        headers["ChatGPT-Account-ID"] = account_id

    try:
        with httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.get(_WHAM_USAGE_URL, headers=headers)
    except httpx.TimeoutException as exc:
        raise _Unavailable("request_timeout") from exc
    except httpx.HTTPError as exc:
        raise _Unavailable(f"request_failed:{type(exc).__name__}") from exc

    if response.status_code != 200:
        raise _Unavailable(f"http_status_{response.status_code}")
    try:
        payload = response.json()
    except (ValueError, UnicodeError) as exc:
        raise _Unavailable("response_invalid_json") from exc
    if not isinstance(payload, Mapping):
        raise _Unavailable("response_malformed")
    return payload


def _variant(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _valid_percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 100.0:
        return None
    return numeric


def _remaining_percent(used: float | None) -> float | None:
    if used is None:
        return None
    return max(0.0, 100.0 - used)


def _valid_duration(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        return None
    return numeric


def _reset_iso(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0.0:
        return None
    try:
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _window(limit: Mapping[str, Any], kind: str) -> _Window:
    raw = _variant(limit, f"{kind}_window", kind)
    if not isinstance(raw, Mapping):
        return _Window(None, None, None, None)

    name = _variant(raw, "window_name", "windowName")
    if not isinstance(name, str):
        name = None
    duration = _valid_duration(_variant(raw, "window_duration_mins", "windowDurationMins"))
    if not any(key in raw for key in ("window_duration_mins", "windowDurationMins")):
        seconds = _valid_duration(raw.get("limit_window_seconds"))
        duration = seconds / 60.0 if seconds is not None else None
    return _Window(
        used_percent=_valid_percent(_variant(raw, "used_percent", "usedPercent")),
        reset_at=_reset_iso(_variant(raw, "reset_at", "resetsAt")),
        name=name,
        duration_mins=duration,
    )


def _rate_limit(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    malformed = False
    for key in ("rate_limit", "rateLimits"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, Mapping):
            return value
        malformed = True
    if malformed:
        raise _Unavailable("quota_fields_malformed")
    raise _Unavailable("quota_fields_missing")


def read_quota(
    auth_path: str | Path,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = _TIMEOUT_SECONDS,
) -> QuotaResult:
    """Read one account's current Codex OAuth rate-limit usage.

    This function is fail-soft and never exposes provider bodies, credential
    values, or the auth path in its errors. Every failure returns current
    ``status="unavailable"`` data with all quota facts unknown.
    """
    observed_at = _now_iso()
    path = Path(auth_path).expanduser()
    if not path.is_file():
        return _unknown(observed_at, "auth_file_not_found")

    try:
        access_token, account_id = _read_existing_auth(path)
        payload = _request_usage(
            access_token,
            account_id,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        limit = _rate_limit(payload)
        primary = _window(limit, "primary")
        secondary = _window(limit, "secondary")
    except _Unavailable as exc:
        return _unknown(observed_at, str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-soft boundary; never raise
        return _unknown(observed_at, f"unexpected_error:{type(exc).__name__}")

    return QuotaResult(
        primary.used_percent,
        secondary.used_percent,
        primary.reset_at,
        secondary.reset_at,
        observed_at,
        status="ok",
        primary_window_name=primary.name,
        secondary_window_name=secondary.name,
        primary_window_duration_mins=primary.duration_mins,
        secondary_window_duration_mins=secondary.duration_mins,
    )
