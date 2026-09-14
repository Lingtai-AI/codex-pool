"""Read one Codex account's current quota from the read-only WHAM endpoint.

The quota command reads the account's existing flat auth file, uses Codex's
existing file-locked token refresh owner only when the access token needs it,
and makes one direct ``GET https://chatgpt.com/backend-api/wham/usage``
request. It never starts a Codex subprocess, copies tokens to a temporary
home, retries, or falls back to another account or transport.

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
import time
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .auth_codex import CodexRequestCancelled, CodexRequestTimeout, CodexTokenManager, request_json

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
    allowed: bool | None = None
    limit_reached: bool | None = None
    secondary_malformed: bool = False

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
        if self.limit_reached is not None:
            return self.limit_reached
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
    present: bool = False
    malformed: bool = False


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


def _quota_token(
    path: Path,
    *,
    transport: httpx.BaseTransport | None,
    timeout_seconds: float,
    deadline: float,
    monotonic_fn: Any,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str | None]:
    """Return a usable token, sharing Codex's file-locked refresh owner.

    Quota has one provider request budget.  A near-expiry token is refreshed
    through the existing auth owner before that one WHAM request; WHAM is
    never retried here.
    """
    access_token, account_id = _read_existing_auth(path)
    if cancel_event is not None and cancel_event.is_set():
        raise _Unavailable("request_timeout")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _Unavailable("auth_read_failed") from exc
    refresh_token = raw.get("refresh_token") if isinstance(raw, Mapping) else None
    if isinstance(refresh_token, str) and refresh_token:
        try:
            manager = CodexTokenManager(
                str(path), transport=transport, time_fn=time.time,
                refresh_timeout=max(0.1, timeout_seconds), deadline=deadline,
                monotonic_fn=monotonic_fn,
                cancel_event=cancel_event,
            )
            access_token = manager.get_access_token()
            account_id = manager.get_account_id() or account_id
            if cancel_event is not None and cancel_event.is_set():
                raise _Unavailable("request_timeout")
        except _Unavailable:
            raise
        except (CodexRequestCancelled, CodexRequestTimeout) as exc:
            raise _Unavailable("request_timeout") from exc
        except Exception as exc:  # auth provider boundary; do not expose details
            raise _Unavailable("auth_refresh_failed") from exc
    return access_token, account_id


def _request_usage(
    access_token: str,
    account_id: str | None,
    *,
    timeout_seconds: float,
    transport: httpx.BaseTransport | None,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
    monotonic_fn: Any = time.monotonic,
) -> Mapping[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if account_id is not None:
        headers["ChatGPT-Account-ID"] = account_id

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise _Unavailable("request_timeout")
        remaining = timeout_seconds if deadline is None else max(0.0, deadline - monotonic_fn())
        if remaining <= 0:
            raise _Unavailable("request_timeout")
        response = request_json(
            "GET",
            _WHAM_USAGE_URL,
            transport=transport,
            timeout_seconds=remaining,
            deadline=deadline,
            monotonic_fn=monotonic_fn,
            cancel_event=cancel_event,
            headers=headers,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise _Unavailable("request_timeout")
        if deadline is not None and monotonic_fn() >= deadline:
            raise _Unavailable("request_timeout")
    except httpx.TimeoutException as exc:
        raise _Unavailable("request_timeout") from exc
    except (CodexRequestCancelled, CodexRequestTimeout) as exc:
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


def _aliased(
    raw: Mapping[str, Any],
    keys: tuple[str, ...],
    normalizer: Any,
) -> tuple[Any, bool, bool]:
    """Normalize every supplied alias and detect invalid/conflicting values."""
    values: list[Any] = []
    malformed = False
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        normalized = normalizer(value)
        if value is not None and normalized is None:
            malformed = True
        values.append(normalized)
    present = bool(values)
    if values and any(value != values[0] for value in values[1:]):
        malformed = True
    return (values[0] if values else None), present, malformed


def _window_mapping(raw: Any, *, strict: bool) -> _Window:
    if not isinstance(raw, Mapping):
        return _Window(None, None, None, None, present=True, malformed=True)

    name, name_present, name_malformed = _aliased(
        raw, ("window_name", "windowName"), lambda value: value if isinstance(value, str) else None
    )
    malformed = strict and (not raw or name_malformed)
    if not isinstance(name, str):
        name = None

    duration_mins, mins_present, mins_malformed = _aliased(
        raw, ("window_duration_mins", "windowDurationMins"), _valid_duration
    )
    seconds, seconds_present, seconds_malformed = _aliased(
        raw, ("limit_window_seconds",), _valid_duration
    )
    duration_present = mins_present or seconds_present
    duration = duration_mins
    if seconds_present:
        seconds_duration = seconds / 60.0 if seconds is not None else None
        if mins_present and duration != seconds_duration:
            mins_malformed = True
        if not mins_present:
            duration = seconds_duration
    if strict and duration_present and (mins_malformed or seconds_malformed):
        malformed = True

    raw_used, used_present, used_malformed = _aliased(
        raw, ("used_percent", "usedPercent"), _valid_percent
    )
    if strict and (not used_present or raw_used is None or used_malformed):
        malformed = True

    reset_raw, reset_present, reset_malformed = _aliased(
        raw, ("reset_at", "resetsAt"), _reset_iso
    )
    if strict and reset_present and reset_malformed:
        malformed = True
    return _Window(
        used_percent=_valid_percent(raw_used),
        reset_at=reset_raw,
        name=name,
        duration_mins=duration,
        present=True,
        malformed=malformed or (not strict and raw_used is not None and _valid_percent(raw_used) is None),
    )


def _window(limit: Mapping[str, Any], kind: str, *, strict: bool = False) -> _Window:
    containers = [
        limit[key]
        for key in (f"{kind}_window", kind)
        if key in limit
    ]
    if not containers:
        return _Window(None, None, None, None)
    # Live WHAM uses an explicit null for an unsupported secondary window.
    # Treat only that single-container shape as absent; conflicting aliases and
    # every other malformed explicit value still fail closed below.
    if strict and len(containers) == 1 and containers[0] is None:
        return _Window(None, None, None, None)
    if not strict or len(containers) == 1:
        return _window_mapping(containers[0], strict=strict)

    normalized = [_window_mapping(container, strict=True) for container in containers]
    if any(window.malformed for window in normalized):
        return _Window(None, None, None, None, present=True, malformed=True)
    first = normalized[0]
    if any(
        (
            window.used_percent != first.used_percent
            or window.reset_at != first.reset_at
            or window.name != first.name
            or window.duration_mins != first.duration_mins
        )
        for window in normalized[1:]
    ):
        return _Window(None, None, None, None, present=True, malformed=True)
    return first


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
    now_fn=_now_iso,
    cancel_event: threading.Event | None = None,
    monotonic_fn: Any = time.monotonic,
    deadline: float | None = None,
) -> QuotaResult:
    """Read one account's current Codex OAuth rate-limit usage.

    This function is fail-soft and never exposes provider bodies, credential
    values, or the auth path in its errors. Every failure returns current
    ``status="unavailable"`` data with all quota facts unknown.
    """
    deadline = deadline if deadline is not None else monotonic_fn() + max(0.0, timeout_seconds)
    observed_at = now_fn()
    path = Path(auth_path).expanduser()
    if not path.is_file():
        return _unknown(observed_at, "auth_file_not_found")

    try:
        access_token, account_id = _quota_token(
            path,
            transport=transport,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            monotonic_fn=monotonic_fn,
            cancel_event=cancel_event,
        )
        # The freshness clock starts immediately before the provider request,
        # rather than when a caller redraws a UI or opens the auth file.
        observed_at = now_fn()
        payload = _request_usage(
            access_token,
            account_id,
            timeout_seconds=timeout_seconds,
            transport=transport,
            deadline=deadline,
            cancel_event=cancel_event,
            monotonic_fn=monotonic_fn,
        )
        limit = _rate_limit(payload)
        primary = _window(limit, "primary")
        secondary = _window(limit, "secondary", strict=True)
        if secondary.malformed:
            return _unknown(observed_at, "quota_fields_malformed")
        allowed = _variant(limit, "allowed", "is_allowed")
        limit_reached = _variant(limit, "limit_reached", "limitReached")
        if not isinstance(allowed, bool):
            allowed = None
        if not isinstance(limit_reached, bool):
            limit_reached = None
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
        allowed=allowed,
        limit_reached=limit_reached,
        secondary_malformed=secondary.malformed,
    )
