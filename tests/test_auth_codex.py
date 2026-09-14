import base64
import gzip
import json

import httpx
import pytest

from subs_pool.modules.codex.auth_codex import (
    CodexAuthError,
    CodexTokenManager,
    is_token_expired_error,
    is_usage_limit_reached_error,
    request_json,
)
from fakes import write_auth_fixture


def test_is_authenticated_true_and_false(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth)
    assert CodexTokenManager(str(auth)).is_authenticated() is True

    missing = tmp_path / "missing.json"
    assert CodexTokenManager(str(missing)).is_authenticated() is False


def test_get_access_token_without_refresh_needed(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=3600)
    assert CodexTokenManager(str(auth)).get_access_token() == "at-1"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"refresh_token": []},
        {"refresh_token": "rt-1", "access_token": {"value": "at-1"}},
        {"refresh_token": "rt-1", "expires_at": "never"},
    ],
)
def test_valid_json_with_invalid_auth_shape_is_unavailable(tmp_path, payload):
    auth = tmp_path / "malformed.json"
    auth.write_text(json.dumps(payload), encoding="utf-8")
    manager = CodexTokenManager(str(auth))
    assert manager.is_authenticated() is False
    assert manager.get_account_id() is None


def test_refresh_triggers_on_near_expiry(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=60)  # inside the 300s refresh buffer

    def handler(request):
        url = str(request.url)
        data = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "rt-1"
        return httpx.Response(
            200,
            json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    mgr = CodexTokenManager(str(auth), transport=httpx.MockTransport(handler))
    token = mgr.get_access_token()
    assert token == "at-2"
    on_disk = json.loads(auth.read_text())
    assert on_disk["access_token"] == "at-2"
    assert on_disk["refresh_token"] == "rt-2"


def test_refresh_401_raises_codex_auth_error(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, expires_in=1)

    def handler(_request):
        return httpx.Response(401, json={"error": "invalid_grant"}, request=httpx.Request("POST", url))

    url = "https://auth.openai.com/oauth/token"
    with pytest.raises(CodexAuthError):
        CodexTokenManager(str(auth), transport=httpx.MockTransport(handler)).get_access_token()


def test_account_id_from_explicit_field(tmp_path):
    auth = tmp_path / "auth.json"
    write_auth_fixture(auth, account_id="explicit-acct")
    assert CodexTokenManager(str(auth)).get_account_id() == "explicit-acct"


def test_account_id_from_id_token_claim(tmp_path):
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "claim-acct"}}).encode()
    ).decode().rstrip("=")
    id_token = f"header.{payload}.sig"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": 9999999999, "id_token": id_token}))
    assert CodexTokenManager(str(auth)).get_account_id() == "claim-acct"


def test_account_id_none_when_absent(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": 9999999999}))
    assert CodexTokenManager(str(auth)).get_account_id() is None


class _Exc(Exception):
    def __init__(self, status_code=None, code=None):
        super().__init__("x")
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


def test_token_expired_classifier_requires_exact_structural_match():
    assert is_token_expired_error(_Exc(status_code=401, code="token_expired")) is True
    assert is_token_expired_error(_Exc(status_code=401, code="something_else")) is False
    assert is_token_expired_error(_Exc(status_code=403, code="token_expired")) is False
    assert is_token_expired_error(Exception("token_expired 401")) is False  # never from message text


def test_usage_limit_classifier_requires_exact_structural_match():
    assert is_usage_limit_reached_error(_Exc(status_code=429, code="usage_limit_reached")) is True
    assert is_usage_limit_reached_error(_Exc(status_code=429, code="rate_limited")) is False
    assert is_usage_limit_reached_error(_Exc(status_code=500, code="usage_limit_reached")) is False


def test_request_json_decodes_gzip_response_once():
    payload = {"rate_limit": {"allowed": True}}
    compressed = gzip.compress(json.dumps(payload).encode("utf-8"))

    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield compressed

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            stream=CompressedStream(),
            request=request,
        )

    response = request_json(
        "GET",
        "https://example.test/usage",
        transport=httpx.MockTransport(handler),
        timeout_seconds=2.0,
    )

    assert response.json() == payload
