"""Mocked coverage of the real HTTP adapter.

Uses injected httpx transports only: no live socket or Codex endpoint. The
suite asserts request shape, SSE decoding, and bounded provider error output.
"""

from __future__ import annotations

import json

import httpx
import pytest

from codex_pool.errors import UpstreamTransportError
from codex_pool.upstream import CODEX_OFFICIAL_BASE_URL, CodexHTTPUpstream


class _ByteAtATimeStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        for i in range(len(self._data)):
            yield self._data[i : i + 1]


def _sse_body(events: list[tuple[str, dict]]) -> bytes:
    body = b""
    for etype, data in events:
        body += f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
    return body


@pytest.mark.asyncio
async def test_stream_posts_to_configured_url_with_expected_headers_and_forces_stream_true():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse_body(
                [("response.completed", {"type": "response.completed", "response": {"id": "r1", "status": "completed", "output": []}})]
            ),
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    events = [
        event
        async for event in upstream.stream(
            access_token="secret-token-abc",
            account_id="acct-9",
            payload={"model": "gpt-5-codex", "input": [], "stream": False},
        )
    ]

    assert captured["url"] == f"{CODEX_OFFICIAL_BASE_URL}/responses"
    assert captured["headers"]["authorization"] == "Bearer secret-token-abc"
    assert captured["headers"]["chatgpt-account-id"] == "acct-9"
    assert captured["headers"]["accept"] == "text/event-stream"
    assert captured["body"]["stream"] is True
    assert events[0]["type"] == "response.completed"


@pytest.mark.asyncio
async def test_stream_omits_account_header_when_account_id_is_none():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            content=_sse_body([("response.completed", {"type": "response.completed", "response": {"status": "completed", "output": []}})]),
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    _ = [event async for event in upstream.stream(access_token="t", account_id=None, payload={"model": "m", "input": []})]
    assert "chatgpt-account-id" not in captured["headers"]


@pytest.mark.asyncio
async def test_stream_never_lets_payload_choose_an_arbitrary_upstream_url():
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            content=_sse_body([("response.completed", {"type": "response.completed", "response": {"status": "completed", "output": []}})]),
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    _ = [
        event
        async for event in upstream.stream(
            access_token="t",
            account_id=None,
            payload={"model": "m", "input": [], "base_url": "https://evil.example/steal", "url": "https://evil.example/steal"},
        )
    ]
    assert seen_urls == [f"{CODEX_OFFICIAL_BASE_URL}/responses"]


@pytest.mark.asyncio
async def test_http_error_is_structured_without_leaking_bearer_or_arbitrary_error_field():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer super-secret-token"
        return httpx.Response(
            401,
            json={
                "error": {
                    "type": "invalid_auth",
                    "code": "token_expired",
                    "message": "token super-secret-token has expired",
                }
            },
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamTransportError) as excinfo:
        async for _ in upstream.stream(access_token="super-secret-token", account_id=None, payload={"model": "m", "input": []}):
            pass

    assert excinfo.value.status_code == 502
    message = str(excinfo.value)
    assert "super-secret-token" not in message
    assert "401" in message
    assert "token_expired" in message or "invalid_auth" in message


@pytest.mark.asyncio
async def test_arbitrary_provider_type_or_code_is_not_echoed():
    secret = "secret-value-should-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"type": secret, "code": secret}})

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamTransportError) as excinfo:
        async for _ in upstream.stream(access_token="t", account_id=None, payload={"model": "m", "input": []}):
            pass
    assert secret not in str(excinfo.value)


@pytest.mark.asyncio
async def test_transport_failure_is_structured_without_leaking_request_details():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer another-secret"
        raise httpx.ConnectError("connection refused another-secret", request=request)

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamTransportError) as excinfo:
        async for _ in upstream.stream(access_token="another-secret", account_id=None, payload={"model": "m", "input": []}):
            pass
    assert "another-secret" not in str(excinfo.value)
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_decodes_multibyte_utf8_split_at_single_byte_transport_reads():
    payload = {"type": "response.output_text.delta", "delta": "你好，世界！🎉"}
    body = f"event: response.output_text.delta\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ByteAtATimeStream(body))

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    events = [event async for event in upstream.stream(access_token="t", account_id=None, payload={"model": "m", "input": []})]
    assert events == [payload]
