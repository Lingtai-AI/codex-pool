from __future__ import annotations

import json
import math
from datetime import datetime

import httpx
import pytest

from fakes import write_auth_fixture

from codex_pool.quota import QuotaResult, read_quota


def test_quota_direct_get_normalizes_snake_case_windows_without_writing_auth(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=-3_600)
    original_auth = auth.read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 30,
                        "reset_at": 1_700_000_000,
                        "window_name": "burst",
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 0,
                        "reset_at": 1_700_000_100,
                        "window_name": "long",
                        "window_duration_mins": 10_080,
                    },
                }
            },
        )

    result = read_quota(auth, transport=httpx.MockTransport(handler))

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://chatgpt.com/backend-api/wham/usage"
    assert request.headers["authorization"] == "Bearer at-1"
    assert request.headers["chatgpt-account-id"] == "acct-1"
    assert request.headers["accept"] == "application/json"
    assert result.status == "ok"
    assert result.primary_used_percent == 30.0
    assert result.primary_remaining_percent == 70.0
    assert result.secondary_used_percent == 0.0
    assert result.secondary_remaining_percent == 100.0
    assert result.primary_reset_at == "2023-11-14T22:13:20+00:00"
    assert result.secondary_reset_at == "2023-11-14T22:15:00+00:00"
    assert result.primary_window_name == "burst"
    assert result.secondary_window_name == "long"
    assert result.primary_window_duration_mins == 300.0
    assert result.secondary_window_duration_mins == 10_080.0
    assert result.error is None
    datetime.fromisoformat(result.observed_at)
    assert auth.read_bytes() == original_auth

    dumped = json.dumps(result.to_dict())
    assert "at-1" not in dumped
    assert "rt-1" not in dumped
    assert "error" not in result.to_dict()


def test_quota_accepts_camel_case_fields_and_legacy_window_shape(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rateLimits": {
                    "primary": {
                        "usedPercent": 42.5,
                        "resetsAt": "1700000000",
                        "windowName": "actual label",
                        "windowDurationMins": 60,
                    }
                }
            },
        )

    result = read_quota(auth, transport=httpx.MockTransport(handler))

    assert result.status == "ok"
    assert result.primary_used_percent == 42.5
    assert result.primary_remaining_percent == 57.5
    assert result.primary_window_name == "actual label"
    assert result.primary_window_duration_mins == 60.0
    assert result.secondary_used_percent is None
    assert result.secondary_remaining_percent is None


def test_quota_does_not_require_refresh_token_or_refresh_missing_account_id(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "at-only"}), encoding="utf-8")
    before = auth.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "chatgpt-account-id" not in request.headers
        return httpx.Response(200, json={"rate_limit": {"primary_window": {"used_percent": 1}}})

    result = read_quota(auth, transport=httpx.MockTransport(handler))

    assert result.status == "ok"
    assert result.primary_used_percent == 1.0
    assert auth.read_bytes() == before


def test_quota_empty_rate_limit_is_successful_unknown_not_transport_error(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"rate_limit": {}})
    )

    result = read_quota(auth, transport=transport)

    assert result.status == "ok"
    assert result.primary_used_percent is None
    assert result.primary_remaining_percent is None
    assert result.exhausted is None
    assert "error" not in result.to_dict()


@pytest.mark.parametrize("invalid", [True, -1, 100.1, math.nan, math.inf])
def test_quota_rejects_invalid_percent_values_as_unknown(tmp_path, invalid):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=json.dumps(
                {"rate_limit": {"primary_window": {"used_percent": invalid}}}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
    )

    result = read_quota(auth, transport=transport)

    assert result.status == "ok"
    assert result.primary_used_percent is None
    assert result.primary_remaining_percent is None


def test_quota_rejects_invalid_duration_reset_and_name_without_inventing_defaults(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 20,
                            "reset_at": -1,
                            "window_name": 123,
                            "window_duration_mins": math.inf,
                        }
                    }
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
    )

    result = read_quota(auth, transport=transport)

    assert result.primary_remaining_percent == 80.0
    assert result.primary_reset_at is None
    assert result.primary_window_name is None
    assert result.primary_window_duration_mins is None


def test_quota_missing_auth_file_is_unavailable_not_zero(tmp_path):
    result = read_quota(tmp_path / "nope.json")

    assert result.status == "unavailable"
    assert result.primary_used_percent is None
    assert result.primary_remaining_percent is None
    assert result.error == "auth_file_not_found"


def test_quota_malformed_auth_file_reports_nonsecret_error(tmp_path):
    auth = tmp_path / "secret-name-account.json"
    auth.write_text("not json", encoding="utf-8")

    result = read_quota(auth)

    assert result.status == "unavailable"
    assert result.primary_used_percent is None
    assert result.error == "auth_read_failed:JSONDecodeError"
    assert "secret-name-account" not in result.error


def test_quota_missing_access_token_is_unavailable_without_request(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"refresh_token": "rt-unused"}), encoding="utf-8")

    result = read_quota(auth)

    assert result.status == "unavailable"
    assert result.error == "auth_access_token_missing"


def test_quota_http_429_is_unavailable_and_is_not_retried(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"sensitive": "provider body is ignored"})

    result = read_quota(auth, transport=httpx.MockTransport(handler))

    assert calls == 1
    assert result.status == "unavailable"
    assert result.primary_used_percent is None
    assert result.secondary_used_percent is None
    assert result.primary_remaining_percent is None
    assert result.primary_window_name is None
    assert result.error == "http_status_429"
    assert "sensitive" not in json.dumps(result.to_dict())


def test_quota_transport_failure_is_unavailable_and_not_retried(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("contains-provider-detail", request=request)

    result = read_quota(auth, transport=httpx.MockTransport(handler))

    assert calls == 1
    assert result.status == "unavailable"
    assert result.error == "request_failed:ConnectError"
    assert "provider-detail" not in result.error


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(200, content=b"not-json"), "response_invalid_json"),
        (httpx.Response(200, json=[]), "response_malformed"),
        (httpx.Response(200, json={}), "quota_fields_missing"),
        (httpx.Response(200, json={"rate_limit": []}), "quota_fields_malformed"),
    ],
)
def test_quota_malformed_responses_are_unavailable(tmp_path, response, error):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)

    result = read_quota(
        auth,
        transport=httpx.MockTransport(lambda _request: response),
    )

    assert result.status == "unavailable"
    assert result.primary_used_percent is None
    assert result.primary_remaining_percent is None
    assert result.error == error


def test_quota_exhaustion_property_is_explicit_and_unknown_is_not_exhausted():
    assert QuotaResult(100.0, None, None, None, "now").exhausted is True
    assert QuotaResult(99.9, None, None, None, "now").exhausted is False
    unavailable = QuotaResult(None, None, None, None, "now", error="read_failed")
    assert unavailable.exhausted is None
    assert unavailable.status == "unavailable"
