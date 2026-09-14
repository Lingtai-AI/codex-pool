from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime

import httpx
import pytest

from fakes import write_auth_fixture

from subs_pool.modules.codex.quota import QuotaResult, read_quota


class _DripStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], clock: list[float], step: float, closed: list[bool]) -> None:
        self.chunks = chunks
        self.clock = clock
        self.step = step
        self.closed = closed

    async def __aiter__(self):
        for chunk in self.chunks:
            self.clock[0] += self.step
            yield chunk

    async def aclose(self) -> None:
        self.closed.append(True)


class _BlockedAfterChunk(httpx.AsyncByteStream):
    def __init__(self, closed: list[bool], *, delay: float = 0.03) -> None:
        self.closed = closed
        self.delay = delay

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield b"{"
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.append(True)


def test_quota_direct_get_normalizes_snake_case_windows_without_writing_auth(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=3_600)
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


def test_expired_access_token_refreshes_once_then_makes_one_wham_request(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=-3600)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + str(request.url))
        if request.url.host == "auth.openai.com":
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer at-2"
        return httpx.Response(200, json={"rate_limit": {"primary_window": {"used_percent": 10}}})

    result = read_quota(auth, transport=httpx.MockTransport(handler), timeout_seconds=8)

    assert result.status == "ok"
    assert calls == ["POST https://auth.openai.com/oauth/token", "GET https://chatgpt.com/backend-api/wham/usage"]


def test_auth_and_wham_share_one_monotonic_budget_and_wham_is_not_retried(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=-3600)
    monotonic = [0.0]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.url.host == "auth.openai.com":
            monotonic[0] = 7.5
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        monotonic[0] = 8.1
        return httpx.Response(200, json={"rate_limit": {"primary_window": {"used_percent": 10}}})

    result = read_quota(
        auth,
        transport=httpx.MockTransport(handler),
        timeout_seconds=8,
        monotonic_fn=lambda: monotonic[0],
    )

    assert calls == ["POST", "GET"]
    assert result.status == "unavailable"
    assert result.error == "request_timeout"


def test_slow_drip_wham_body_is_cut_off_by_absolute_deadline_and_closed(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    clock = [0.0]
    closed: list[bool] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            stream=_DripStream([b'{"rate_limit":', b'{"primary_window":', b'{"used_percent":10}}}'], clock, 3.0, closed),
        )

    result = read_quota(
        auth,
        transport=httpx.MockTransport(handler),
        timeout_seconds=8,
        monotonic_fn=lambda: clock[0],
    )

    assert result.status == "unavailable"
    assert result.error == "request_timeout"
    assert calls == ["GET"]
    assert closed == [True]


def test_slow_drip_token_body_exhausts_budget_without_starting_wham(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=-3600)
    clock = [0.0]
    closed: list[bool] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            stream=_DripStream([b'{"access_token":"at-2",', b'"expires_in":3600}'], clock, 9.0, closed),
        )

    result = read_quota(
        auth,
        transport=httpx.MockTransport(handler),
        timeout_seconds=8,
        monotonic_fn=lambda: clock[0],
    )

    assert result.status == "unavailable"
    assert result.error == "request_timeout"
    assert calls == ["POST"]
    assert closed == [True]


def test_blocked_token_body_is_aborted_at_absolute_deadline_without_wham(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=-3600)
    calls: list[str] = []
    closed: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, stream=_BlockedAfterChunk(closed))

    started = time.monotonic()
    result = read_quota(auth, transport=httpx.MockTransport(handler), timeout_seconds=0.08)
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert result.status == "unavailable"
    assert result.error == "request_timeout"
    assert calls == ["POST"]
    assert closed == [True]


def test_blocked_wham_body_is_aborted_at_absolute_deadline_and_closed(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    calls: list[str] = []
    closed: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, stream=_BlockedAfterChunk(closed))

    started = time.monotonic()
    result = read_quota(auth, transport=httpx.MockTransport(handler), timeout_seconds=0.08)
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert result.status == "unavailable"
    assert result.error == "request_timeout"
    assert calls == ["GET"]
    assert closed == [True]


def test_blocked_headers_are_aborted_at_absolute_deadline(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    calls: list[str] = []
    closed: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)
        raise AssertionError("unreachable")

    started = time.monotonic()
    result = read_quota(auth, transport=httpx.MockTransport(handler), timeout_seconds=0.08)
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert result.status == "unavailable"
    assert result.error == "request_timeout"
    assert calls == ["GET"]
    assert closed == [True]


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


@pytest.mark.parametrize(
    "secondary",
    [
        [],
        "bad",
        0,
        {},
        {"used_percent": None},
        {"used_percent": 10, "reset_at": "not-a-timestamp"},
        {"used_percent": 10, "window_duration_mins": "not-a-duration"},
    ],
)
def test_explicit_malformed_secondary_is_unavailable(tmp_path, secondary):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "rate_limit": {"primary_window": {"used_percent": 10}, "secondary_window": secondary}
    }))

    result = read_quota(auth, transport=transport)

    assert result.status == "unavailable"
    assert result.error == "quota_fields_malformed"


def test_explicit_null_secondary_is_absent(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 28, "limit_window_seconds": 604800},
            "secondary_window": None,
        }
    }))

    result = read_quota(auth, transport=transport)

    assert result.status == "ok"
    assert result.error is None
    assert result.primary_remaining_percent == 72.0
    assert result.primary_window_duration_mins == 10_080.0
    assert result.secondary_used_percent is None
    assert result.secondary_remaining_percent is None
    assert result.allowed is True
    assert result.limit_reached is False


@pytest.mark.parametrize(
    "secondary",
    [
        {"used_percent": 10, "usedPercent": "bad"},
        {"reset_at": 1_700_000_000, "resetsAt": "bad", "used_percent": 10},
        {"window_duration_mins": 60, "windowDurationMins": "bad", "used_percent": 10},
        {"used_percent": 10, "usedPercent": 20},
        {"window_duration_mins": 60, "limit_window_seconds": 7200, "used_percent": 10},
    ],
)
def test_explicit_secondary_alias_conflicts_or_invalid_alternate_is_unavailable(tmp_path, secondary):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "rate_limit": {"primary_window": {"used_percent": 10}, "secondary_window": secondary}
    }))

    result = read_quota(auth, transport=transport)

    assert result.status == "unavailable"
    assert result.error == "quota_fields_malformed"


def test_explicit_secondary_equal_aliases_are_unambiguous(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "rate_limit": {
            "primary_window": {"used_percent": 10},
            "secondary_window": {
                "used_percent": 10, "usedPercent": 10.0,
                "reset_at": 1_700_000_000, "resetsAt": "1700000000",
                "window_duration_mins": 60, "windowDurationMins": 60.0,
                "limit_window_seconds": 3600,
            },
        }
    }))

    result = read_quota(auth, transport=transport)

    assert result.status == "ok"
    assert result.secondary_used_percent == 10.0
    assert result.secondary_window_duration_mins == 60.0


@pytest.mark.parametrize(
    ("containers", "expected"),
    [
        (
            {"secondary_window": {"used_percent": 10}, "secondary": {"used_percent": "bad"}},
            "quota_fields_malformed",
        ),
        (
            {"secondary_window": {"used_percent": 10}, "secondary": None},
            "quota_fields_malformed",
        ),
        (
            {"secondary_window": {"used_percent": 10}, "secondary": []},
            "quota_fields_malformed",
        ),
        (
            {"secondary_window": {"used_percent": 10}, "secondary": "bad"},
            "quota_fields_malformed",
        ),
        (
            {"secondary_window": {"used_percent": 10}, "secondary": {}},
            "quota_fields_malformed",
        ),
        (
            {"secondary_window": {"used_percent": 10}, "secondary": {"used_percent": 20}},
            "quota_fields_malformed",
        ),
        (
            {
                "secondary_window": {"used_percent": 10, "reset_at": 1_700_000_000},
                "secondary": {"used_percent": 10, "reset_at": 1_700_000_100},
            },
            "quota_fields_malformed",
        ),
        (
            {
                "secondary_window": {"used_percent": 10, "window_duration_mins": 60},
                "secondary": {"used_percent": 10, "window_duration_mins": 120},
            },
            "quota_fields_malformed",
        ),
        (
            {
                "secondary_window": {"used_percent": 10, "window_name": "short"},
                "secondary": {"used_percent": 10, "window_name": "long"},
            },
            "quota_fields_malformed",
        ),
        (
            {"secondary_window": {"used_percent": 10}, "secondary": {"usedPercent": 10.0}},
            None,
        ),
        ({"secondary_window": {"used_percent": 10}}, None),
        ({}, None),
    ],
)
def test_secondary_outer_container_aliases_are_all_validated(tmp_path, containers, expected):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={
        "rate_limit": {"primary_window": {"used_percent": 10}, **containers}
    }))

    result = read_quota(auth, transport=transport)

    assert result.status == ("ok" if expected is None else "unavailable")
    assert result.error == expected


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
