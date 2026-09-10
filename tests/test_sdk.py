"""Ordinary ``openai.AsyncOpenAI`` round-trips against the ASGI app.

The real SDK request and response/SSE parsing run against an in-process
``httpx.ASGITransport``. No socket or live Codex endpoint is used. The parent
installs openai in its test environment; this module skips if unavailable.
"""

from __future__ import annotations

import httpx
import pytest

openai = pytest.importorskip("openai")

from codex_pool.accounts import AccountStore
from codex_pool.chain import ChainStore
from codex_pool.server import create_app
from fakes import ScriptedUpstream, write_auth_fixture

API_KEY = "test-key-do-not-log"


def _setup_one_account(tmp_path):
    auth = tmp_path / "personal.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("personal", str(auth))
    return store


def _sdk_client(app, *, api_key: str = API_KEY) -> "openai.AsyncOpenAI":
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://local/v1")
    return openai.AsyncOpenAI(
        api_key=api_key,
        base_url="http://local/v1",
        http_client=http_client,
        max_retries=0,
    )


@pytest.mark.asyncio
async def test_sdk_create_with_plain_string_input_parses_output_text_and_full_dump(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "hi there", "annotations": []}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)
    client = _sdk_client(app)

    response = await client.responses.create(model="gpt-5-codex", input="hello")

    assert response.output_text == "hi there"
    assert response.status == "completed"
    dumped = response.model_dump(exclude_none=True)
    assert dumped["output"][0]["content"][0]["text"] == "hi there"
    assert upstream.calls[0]["payload"]["input"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_sdk_create_parses_function_call_and_reasoning_items_and_usage(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "thinking it through"}],
        "status": "completed",
    }
    tool_item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "search",
        "arguments": '{"q": "weather"}',
        "status": "completed",
    }
    upstream.queue_success(model="gpt-5-codex", output=[reasoning_item, tool_item])
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)
    client = _sdk_client(app)

    response = await client.responses.create(
        model="gpt-5-codex",
        input=[{"role": "user", "content": "search something"}],
    )

    dumped = response.model_dump(exclude_none=True)
    assert [item["type"] for item in dumped["output"]] == ["reasoning", "function_call"]
    assert dumped["output"][1]["arguments"] == '{"q": "weather"}'
    assert dumped["output"][1]["call_id"] == "call_1"
    assert response.usage.input_tokens >= 0
    assert response.usage.output_tokens >= 0


@pytest.mark.asyncio
async def test_sdk_streaming_create_yields_incremental_text_deltas_and_completed_event(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "id": "msg_2", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "hello world", "annotations": []}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)
    client = _sdk_client(app)

    deltas: list[str] = []
    completed = None
    stream = await client.responses.create(
        model="gpt-5-codex",
        input=[{"role": "user", "content": "hello"}],
        stream=True,
    )
    async for event in stream:
        if event.type == "response.output_text.delta":
            deltas.append(event.delta)
        elif event.type == "response.completed":
            completed = event.response

    assert len(deltas) > 1
    assert "".join(deltas) == "hello world"
    assert completed is not None
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_sdk_wrong_api_key_raises_authentication_error(tmp_path):
    accounts = _setup_one_account(tmp_path)
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=ScriptedUpstream(), api_key=API_KEY)
    client = _sdk_client(app, api_key="wrong-key")

    with pytest.raises(openai.AuthenticationError):
        await client.responses.create(model="gpt-5-codex", input="hello")


@pytest.mark.asyncio
async def test_sdk_unsupported_field_raises_bad_request_error(tmp_path):
    accounts = _setup_one_account(tmp_path)
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=ScriptedUpstream(), api_key=API_KEY)
    client = _sdk_client(app)

    with pytest.raises(openai.BadRequestError):
        await client.responses.create(model="gpt-5-codex", input="hello", previous_response_id="resp_123")


@pytest.mark.asyncio
async def test_sdk_store_false_is_accepted_and_forwarded_as_false_upstream(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "id": "msg_3", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)
    client = _sdk_client(app)

    response = await client.responses.create(model="gpt-5-codex", input="hello", store=False)

    assert response.status == "completed"
    assert upstream.calls[0]["payload"]["store"] is False
