"""Mocked coverage of the real HTTP adapter.

Uses injected httpx transports only: no live socket or Codex endpoint. The
suite asserts request shape, SSE decoding, and bounded provider error output.
"""

from __future__ import annotations

import json

import httpx
import pytest

from subs_pool.modules.codex.errors import UpstreamTransportError
from subs_pool.modules.codex.upstream import CODEX_OFFICIAL_BASE_URL, CodexHTTPUpstream


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
    # Native Codex REST wire (not the WebSocket-only beta value, and not the
    # generic SDK `text/event-stream` Accept) — see `CodexHTTPUpstream.stream`.
    assert captured["headers"]["accept"] == "application/json"
    assert "openai-beta" not in captured["headers"]
    assert captured["body"]["stream"] is True
    assert events[0]["type"] == "response.completed"


@pytest.mark.asyncio
async def test_stream_retains_honest_codex_pool_wire_identity_never_lingtai_or_official_cli():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            content=_sse_body([("response.completed", {"type": "response.completed", "response": {"status": "completed", "output": []}})]),
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    _ = [event async for event in upstream.stream(access_token="t", account_id=None, payload={"model": "m", "input": []})]

    assert captured["headers"]["originator"] == "codex-pool"
    assert captured["headers"]["user-agent"].startswith("codex-pool/")
    assert "lingtai" not in captured["headers"]["user-agent"].lower()
    assert "codex_cli_rs" not in captured["headers"]["user-agent"]


@pytest.mark.asyncio
async def test_stream_sends_underscored_session_and_thread_id_headers_with_matching_body_key():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
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
            payload={"model": "m", "input": [], "prompt_cache_key": "chain-abc"},
            session_id="chain-abc",
            thread_id="chain-abc",
        )
    ]

    assert captured["headers"]["session_id"] == "chain-abc"
    assert captured["headers"]["thread_id"] == "chain-abc"
    assert captured["headers"]["x-codex-window-id"] == "chain-abc:0"
    assert "session-id" not in captured["headers"]
    assert "thread-id" not in captured["headers"]
    metadata = json.loads(captured["headers"]["x-codex-turn-metadata"])
    assert metadata["session_id"] == "chain-abc"
    assert metadata["thread_id"] == "chain-abc"
    assert isinstance(metadata["turn_id"], str) and metadata["turn_id"]
    assert isinstance(metadata["turn_started_at_unix_ms"], int)
    assert captured["body"]["prompt_cache_key"] == "chain-abc"


@pytest.mark.asyncio
async def test_stream_omits_identity_headers_when_no_session_or_thread_id_given():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            content=_sse_body([("response.completed", {"type": "response.completed", "response": {"status": "completed", "output": []}})]),
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    _ = [event async for event in upstream.stream(access_token="t", account_id=None, payload={"model": "m", "input": []})]

    for header in ("session_id", "thread_id", "x-codex-window-id", "x-codex-turn-metadata", "x-client-request-id"):
        assert header not in captured["headers"]


@pytest.mark.asyncio
async def test_stream_generates_fresh_turn_id_and_request_id_each_call():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return httpx.Response(
            200,
            content=_sse_body([("response.completed", {"type": "response.completed", "response": {"status": "completed", "output": []}})]),
        )

    upstream = CodexHTTPUpstream(transport=httpx.MockTransport(handler))
    for _ in range(2):
        _ = [
            event
            async for event in upstream.stream(
                access_token="t",
                account_id=None,
                payload={"model": "m", "input": []},
                session_id="stable-id",
                thread_id="stable-id",
            )
        ]

    first_meta = json.loads(captured[0]["x-codex-turn-metadata"])
    second_meta = json.loads(captured[1]["x-codex-turn-metadata"])
    assert first_meta["turn_id"] != second_meta["turn_id"]
    assert captured[0]["x-client-request-id"] != captured[1]["x-client-request-id"]
    # The window/session identity itself stays the single stable value.
    assert captured[0]["session_id"] == captured[1]["session_id"] == "stable-id"


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
