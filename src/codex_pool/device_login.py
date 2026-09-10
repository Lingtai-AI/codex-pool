"""Codex device-code login — the one supported non-interactive login mode.

Adapted from the LingTai TUI's ``startDeviceAuthFlow`` /
``requestCodexDeviceCode`` / ``pollCodexDeviceAuth`` / ``completeCodexDeviceAuth``
(``internal/tui/oauth.go``, Apache License, Version 2.0; see ``NOTICE``).
Same endpoints, ``client_id``, poll rules, and timeouts as that real,
already-shipping flow:

- ``POST {issuer}/api/accounts/deviceauth/usercode`` — obtain
  ``device_auth_id`` + ``user_code`` (+ optional server ``interval``).
- Poll ``POST {issuer}/api/accounts/deviceauth/token`` every ``interval``
  seconds; HTTP 403/404 means "not approved yet", anything else 2xx/error is
  terminal. Overall poll budget is 15 minutes (``deviceAuthTimeout`` in the Go
  source), matching this module's ``DEVICE_AUTH_TIMEOUT_SECONDS``.
- Exchange the returned ``authorization_code`` + ``code_verifier`` at
  ``{issuer}/oauth/token`` (``grant_type=authorization_code``,
  ``redirect_uri={issuer}/deviceauth/callback``) for the token bundle.

The source ``oauth.go`` also implements a *browser* PKCE flow
(``startOAuthFlow``: local HTTP listener + system browser + localhost
callback). That flow is not headless-CLI-shaped (it needs a bound local port
and a real browser), so it is intentionally not ported here — only the
device-code flow, which is what the frontend contract pins (``accounts login
REF --device``). If browser-based login is ever required, it needs a real UX
decision, not silent invention.

No secret (token, code_verifier, refresh_token) ever leaves this module in an
event dict — only the values the frontend contract allows are yielded.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator, Protocol

import httpx
from filelock import FileLock

from .accounts import Account, AccountError, AccountStore
from .home import data_home

ISSUER_URL = "https://auth.openai.com"
TOKEN_URL = f"{ISSUER_URL}/oauth/token"
DEVICE_USERCODE_URL = f"{ISSUER_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER_URL}/api/accounts/deviceauth/token"
DEVICE_REDIRECT_URI = f"{ISSUER_URL}/deviceauth/callback"
VERIFICATION_URI = f"{ISSUER_URL}/codex/device"

# Same public shared client_id used by the Codex CLI / LingTai TUI / Hermes /
# OpenClaw (oauth.go:45); also duplicated in auth_codex.CLIENT_ID for the
# refresh-token grant.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

DEVICE_AUTH_TIMEOUT_SECONDS = 15 * 60  # oauth.go deviceAuthTimeout
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_NOT_APPROVED_STATUS_CODES = (403, 404)


class DeviceLoginError(Exception):
    """Nonsecret, user-facing device-login failure (safe to print/JSON-encode)."""


class HTTPClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response: ...


def _post_json(client: HTTPClient, url: str, payload: dict, *, timeout: float = 15.0) -> httpx.Response:
    try:
        return client.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
    except httpx.HTTPError as exc:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        raise DeviceLoginError(f"network error contacting {host}") from exc


def _parse_interval(raw: Any) -> float:
    if raw is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_POLL_INTERVAL_SECONDS


def request_device_code(client: HTTPClient) -> dict:
    """``POST {issuer}/api/accounts/deviceauth/usercode`` (oauth.go requestCodexDeviceCode)."""
    resp = _post_json(client, DEVICE_USERCODE_URL, {"client_id": CLIENT_ID})
    if resp.status_code == 404:
        raise DeviceLoginError("device code login is not enabled for this Codex server; use browser OAuth")
    if not (200 <= resp.status_code < 300):
        raise DeviceLoginError(f"device code request failed with status {resp.status_code}")
    try:
        raw = resp.json()
    except ValueError as exc:
        raise DeviceLoginError("device code response was not valid JSON") from exc
    if not isinstance(raw, dict):
        raise DeviceLoginError("device code response was not a JSON object")
    device_auth_id = raw.get("device_auth_id")
    user_code = raw.get("user_code") or raw.get("usercode")
    if not device_auth_id or not user_code:
        raise DeviceLoginError("device code response missing device_auth_id or user_code")
    return {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "interval": _parse_interval(raw.get("interval")),
    }


def poll_device_code(
    client: HTTPClient,
    device_auth_id: str,
    user_code: str,
    *,
    interval: float,
    deadline: float,
    sleep=time.sleep,
    now=time.monotonic,
) -> dict:
    """Poll until approval, denial, or ``deadline`` (oauth.go pollCodexDeviceAuth)."""
    while True:
        resp = _post_json(client, DEVICE_TOKEN_URL, {"device_auth_id": device_auth_id, "user_code": user_code})
        if 200 <= resp.status_code < 300:
            try:
                raw = resp.json()
            except ValueError as exc:
                raise DeviceLoginError("device auth response was not valid JSON") from exc
            if not isinstance(raw, dict):
                raise DeviceLoginError("device auth response was not a JSON object")
            code = raw.get("authorization_code")
            verifier = raw.get("code_verifier")
            if not code or not verifier:
                raise DeviceLoginError("device auth response missing authorization_code or code_verifier")
            return {"authorization_code": code, "code_verifier": verifier}
        if resp.status_code in _NOT_APPROVED_STATUS_CODES:
            if now() >= deadline:
                raise DeviceLoginError(f"device authorization expired after {DEVICE_AUTH_TIMEOUT_SECONDS}s without approval")
            sleep(interval)
            continue
        raise DeviceLoginError(f"device authorization was denied (status {resp.status_code})")


def exchange_code(client: HTTPClient, authorization_code: str, code_verifier: str) -> dict:
    """``POST {issuer}/oauth/token`` (oauth.go exchangeCodeForTokens, device redirect_uri)."""
    form = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": authorization_code,
        "code_verifier": code_verifier,
        "redirect_uri": DEVICE_REDIRECT_URI,
    }
    try:
        resp = client.post(TOKEN_URL, data=form, timeout=30.0)
    except httpx.HTTPError as exc:
        raise DeviceLoginError("network error contacting the Codex token endpoint") from exc
    if resp.status_code != 200:
        raise DeviceLoginError(f"token endpoint returned status {resp.status_code}")
    try:
        raw = resp.json()
    except ValueError as exc:
        raise DeviceLoginError("token response was not valid JSON") from exc
    if not isinstance(raw, dict):
        raise DeviceLoginError("token response was not a JSON object")
    access_token = raw.get("access_token")
    refresh_token = raw.get("refresh_token")
    if not access_token or not refresh_token:
        raise DeviceLoginError("token response missing access_token or refresh_token")
    try:
        expires_in = int(raw.get("expires_in"))
    except (TypeError, ValueError):
        expires_in = 3600
    bundle: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + expires_in,
    }
    id_token = raw.get("id_token")
    if isinstance(id_token, str) and id_token:
        bundle["id_token"] = id_token
    return bundle


def _auth_file_path(ref: str) -> Path:
    auth_dir = data_home() / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    return auth_dir / f"{ref}.json"


def _write_auth_file_atomic(path: Path, bundle: dict) -> None:
    """Atomic (tmp+rename, 0600) write, locked with the same ``.json.lock``
    convention ``auth_codex.CodexTokenManager`` uses on this same path — a
    concurrent token refresh and a re-login on the same account serialize
    rather than race."""
    lock_path = path.with_suffix(".json.lock")
    with FileLock(str(lock_path), timeout=30):
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)
        tmp.replace(path)


def run_device_login(
    ref: str,
    *,
    client: HTTPClient,
    account_store: AccountStore | None = None,
    weight: int = 1,
    sleep=time.sleep,
    now=time.monotonic,
) -> Iterator[dict]:
    """Drive the full device-code flow, yielding frontend-contract JSONL events.

    Yields ``{"event": "authorization_required", ...}`` once the code is
    issued, then (after this generator is driven to completion) a final
    ``{"event": "completed", "account": {...status...}}``. Raises
    :class:`DeviceLoginError` (or :class:`~codex_pool.accounts.AccountError`)
    on any failure — callers must not treat partial iteration as success.

    Re-running login for an existing ``ref`` preserves that account's current
    ``enabled``/``weight`` pool state and only replaces its auth file
    pointer; ``weight`` only applies when ``ref`` is new.
    """
    if not ref or "/" in ref or "\\" in ref or ref in {".", ".."}:
        raise AccountError(f"invalid account ref: {ref!r}")

    store = account_store or AccountStore()

    device = request_device_code(client)
    yield {
        "event": "authorization_required",
        "verification_uri": VERIFICATION_URI,
        "user_code": device["user_code"],
        "expires_in": DEVICE_AUTH_TIMEOUT_SECONDS,
        "interval": int(device["interval"]),
    }

    deadline = now() + DEVICE_AUTH_TIMEOUT_SECONDS
    approval = poll_device_code(
        client,
        device["device_auth_id"],
        device["user_code"],
        interval=device["interval"],
        deadline=deadline,
        sleep=sleep,
        now=now,
    )
    bundle = exchange_code(client, approval["authorization_code"], approval["code_verifier"])

    path = _auth_file_path(ref)
    _write_auth_file_atomic(path, bundle)

    try:
        existing = store.get(ref)
    except AccountError:
        existing = None
    if existing is not None:
        account: Account = store.set_auth_path(ref, str(path))
    else:
        account = store.import_account(ref, str(path), weight=weight)

    yield {"event": "completed", "account": account.to_status_dict()}
