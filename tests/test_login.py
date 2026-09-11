from __future__ import annotations

import json

import httpx
import pytest

from subs_pool.modules.codex.accounts import AccountError, AccountStore
from subs_pool.modules.codex.device_login import (
    DEVICE_AUTH_TIMEOUT_SECONDS,
    DEVICE_TOKEN_URL,
    DEVICE_USERCODE_URL,
    TOKEN_URL,
    VERIFICATION_URI,
    DeviceLoginError,
    run_device_login,
)


class FakeClient:
    """DI HTTP client stub: queues one canned ``httpx.Response`` per call, keyed by URL."""

    def __init__(self, responses_by_url: dict[str, list[httpx.Response]]) -> None:
        self._queues = {url: list(resps) for url, resps in responses_by_url.items()}
        self.calls: list[dict] = []

    def post(self, url, *, json=None, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "data": data, "headers": headers})
        queue = self._queues.get(url)
        if not queue:
            raise AssertionError(f"FakeClient: no more queued responses for {url}")
        return queue.pop(0)


def _resp(status_code: int, payload: dict | None = None) -> httpx.Response:
    req = httpx.Request("POST", "https://auth.openai.com/x")
    if payload is None:
        return httpx.Response(status_code, request=req)
    return httpx.Response(status_code, json=payload, request=req)


def _no_sleep(_seconds: float) -> None:
    pass


def _steady_clock():
    return 0.0


def _usercode_payload(interval=None):
    payload = {"device_auth_id": "dev-1", "user_code": "ABCD-1234"}
    if interval is not None:
        payload["interval"] = interval
    return payload


def test_device_login_success_flow(tmp_path):
    client = FakeClient(
        {
            DEVICE_USERCODE_URL: [_resp(200, _usercode_payload(interval=1))],
            DEVICE_TOKEN_URL: [
                _resp(404),  # not approved yet
                _resp(200, {"authorization_code": "code-1", "code_verifier": "verifier-1"}),
            ],
            TOKEN_URL: [
                _resp(200, {"access_token": "at-1", "refresh_token": "rt-1", "id_token": "hdr.eyJhIjoxfQ.sig", "expires_in": 3600}),
            ],
        }
    )

    events = list(run_device_login("work", client=client, weight=3, sleep=_no_sleep, now=_steady_clock))

    assert events[0] == {
        "event": "authorization_required",
        "verification_uri": VERIFICATION_URI,
        "user_code": "ABCD-1234",
        "expires_in": DEVICE_AUTH_TIMEOUT_SECONDS,
        "interval": 1,
    }
    assert events[1]["event"] == "completed"
    account = events[1]["account"]
    assert account["ref"] == "work"
    assert account["weight"] == 3
    assert account["enabled"] is True
    assert account["auth_present"] is True

    # No secret ever appears in the emitted event stream.
    dumped = json.dumps(events)
    assert "at-1" not in dumped
    assert "rt-1" not in dumped
    assert "code-1" not in dumped
    assert "verifier-1" not in dumped

    # Persisted through the account store, atomically, under this package's own home.
    stored = AccountStore().get("work")
    on_disk = json.loads(open(stored.auth_path).read())
    assert on_disk["access_token"] == "at-1"
    assert on_disk["refresh_token"] == "rt-1"


def test_device_login_relogin_preserves_pool_state(tmp_path):
    auth = tmp_path / "old-auth.json"
    auth.write_text(json.dumps({"access_token": "old", "refresh_token": "old-rt", "expires_at": 0}))
    store = AccountStore()
    store.import_account("work", str(auth), weight=7)
    store.set_enabled("work", False)

    client = FakeClient(
        {
            DEVICE_USERCODE_URL: [_resp(200, _usercode_payload())],
            DEVICE_TOKEN_URL: [_resp(200, {"authorization_code": "c", "code_verifier": "v"})],
            TOKEN_URL: [_resp(200, {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600})],
        }
    )

    events = list(run_device_login("work", client=client, account_store=store, sleep=_no_sleep, now=_steady_clock))
    account = events[-1]["account"]
    assert account["weight"] == 7
    assert account["enabled"] is False


def test_device_login_denied_raises(tmp_path):
    client = FakeClient(
        {
            DEVICE_USERCODE_URL: [_resp(200, _usercode_payload())],
            DEVICE_TOKEN_URL: [_resp(400, {"error": "access_denied"})],
        }
    )
    with pytest.raises(DeviceLoginError, match="denied"):
        list(run_device_login("work", client=client, sleep=_no_sleep, now=_steady_clock))


def test_device_login_expires_before_approval(tmp_path):
    # A clock that jumps far ahead on every call so the deadline is exceeded
    # right after the first "not approved yet" response.
    calls = {"n": 0}

    def jumpy_clock():
        calls["n"] += 1
        return calls["n"] * 2000.0

    client = FakeClient(
        {
            DEVICE_USERCODE_URL: [_resp(200, _usercode_payload())],
            DEVICE_TOKEN_URL: [_resp(404)],
        }
    )
    with pytest.raises(DeviceLoginError, match="expired"):
        list(run_device_login("work", client=client, sleep=_no_sleep, now=jumpy_clock))


def test_device_login_not_enabled_on_server(tmp_path):
    client = FakeClient({DEVICE_USERCODE_URL: [_resp(404)]})
    with pytest.raises(DeviceLoginError, match="not enabled"):
        list(run_device_login("work", client=client, sleep=_no_sleep, now=_steady_clock))


def test_device_login_network_error_wrapped(tmp_path):
    class BoomClient:
        def post(self, url, **kwargs):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    with pytest.raises(DeviceLoginError, match="network error"):
        list(run_device_login("work", client=BoomClient(), sleep=_no_sleep, now=_steady_clock))


def test_device_login_rejects_invalid_ref_before_any_network_call(tmp_path):
    client = FakeClient({})  # no queued responses — must never be called
    with pytest.raises(AccountError):
        list(run_device_login("a/b", client=client, sleep=_no_sleep, now=_steady_clock))
    assert client.calls == []


def test_device_login_token_exchange_missing_fields_raises(tmp_path):
    client = FakeClient(
        {
            DEVICE_USERCODE_URL: [_resp(200, _usercode_payload())],
            DEVICE_TOKEN_URL: [_resp(200, {"authorization_code": "c", "code_verifier": "v"})],
            TOKEN_URL: [_resp(200, {"access_token": "at-only"})],
        }
    )
    with pytest.raises(DeviceLoginError, match="missing access_token or refresh_token"):
        list(run_device_login("work", client=client, sleep=_no_sleep, now=_steady_clock))
