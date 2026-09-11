import asyncio
import json
import time

import httpx
import pytest

from codex_pool.accounts import AccountStore
from codex_pool.chain import ChainStore
from codex_pool.server import create_app, _extract_config, _with_encrypted_reasoning_include
from fakes import ScriptedUpstream, write_auth_fixture

API_KEY = "test-key-do-not-log"


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://local")


def _setup_one_account(tmp_path):
    auth = tmp_path / "personal.json"
    write_auth_fixture(auth)
    store = AccountStore()
    store.import_account("personal", str(auth))
    return store


@pytest.mark.asyncio
async def test_nonstream_create_returns_full_response(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi there"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output_text"] == "hi there"
    assert upstream.calls[0]["payload"]["stream"] is True
    assert upstream.calls[0]["account_id"] == "acct-1"


@pytest.mark.asyncio
async def test_two_turn_plain_assistant_replay_reuses_one_current_record(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    first_output = [{
        "type": "message",
        "id": "msg_0",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }]
    second_output = [{
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "second reply", "annotations": []}],
    }]
    upstream.queue_success(model="gpt-5-codex", output=first_output)
    upstream.queue_success(model="gpt-5-codex", output=second_output)
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        first = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
        second_input = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "continue"},
        ]
        second = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": second_input},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert chain_store.record_count() == 1
    assert upstream.calls[1]["payload"]["input"] == second_input


@pytest.mark.asyncio
async def test_streaming_create_yields_incremental_events(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello world"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        async with client.stream(
            "POST",
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            raw = b""
            async for chunk in resp.aiter_bytes():
                raw += chunk

    text = raw.decode()
    event_types = [line[len("event: "):] for line in text.splitlines() if line.startswith("event: ")]
    assert "response.created" in event_types
    assert event_types.count("response.output_text.delta") > 1
    assert "response.completed" in event_types


@pytest.mark.asyncio
async def test_nonstream_output_items_and_baseline_recorded_on_success(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    tool_item = {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": "{}"}
    reasoning_item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}
    upstream.queue_success(model="gpt-5-codex", output=[reasoning_item, tool_item])
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "search something"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["output"] == [reasoning_item, tool_item]
    assert chain_store.record_count() == 1


@pytest.mark.asyncio
async def test_partial_stream_does_not_advance_baseline_or_fake_completion(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_partial_then_fail(
        model="gpt-5-codex",
        partial_item={"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "starting..."}]},
    )
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 502
    assert "error" in resp.json()
    assert chain_store.record_count() == 0


@pytest.mark.asyncio
async def test_transport_error_before_any_output_returns_error_and_no_commit(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_transport_error()
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 502
    assert chain_store.record_count() == 0
    assert len(upstream.calls) == 1


@pytest.mark.asyncio
async def test_wrong_or_missing_bearer_key_is_rejected_and_key_never_echoed(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp_missing = await client.post("/v1/responses", json={"model": "m", "input": []})
        resp_wrong = await client.post(
            "/v1/responses", headers={"Authorization": "Bearer nope"}, json={"model": "m", "input": []}
        )
    assert resp_missing.status_code == 401
    assert resp_wrong.status_code == 401
    assert API_KEY not in resp_wrong.text
    assert len(upstream.calls) == 0


@pytest.mark.asyncio
async def test_unsupported_field_rejected_loudly_not_silently_dropped(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "m", "input": [], "store": True},
        )
    assert resp.status_code == 400
    assert "store" in resp.json()["error"]["message"]
    assert len(upstream.calls) == 0


@pytest.mark.asyncio
async def test_no_eligible_account_returns_503(tmp_path):
    accounts = AccountStore()
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=ScriptedUpstream(), api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "m", "input": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_output_assembled_from_output_item_done_when_completed_response_omits_output(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    reasoning_item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}
    tool_item = {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": "{}"}
    upstream.queue_success_output_via_done_events_only(model="gpt-5-codex", output=[reasoning_item, tool_item])
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "search something"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["output"] == [reasoning_item, tool_item]
    assert chain_store.record_count() == 1


@pytest.mark.asyncio
async def test_empty_completed_trailer_does_not_discard_done_items(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "kept"}]}
    # A provider may stream the item and then send an empty completion trailer.
    async def stream(**kwargs):
        upstream.calls.append(kwargs)
        yield {"type": "response.output_item.done", "output_index": 0, "item": item}
        yield {
            "type": "response.completed",
            "response": {"id": "r-empty-trailer", "model": "gpt-5-codex", "status": "completed", "output": []},
        }
    upstream.stream = stream
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["output"] == [item]
    assert chain_store.record_count() == 1


@pytest.mark.asyncio
async def test_completed_with_unobservable_output_is_returned_but_never_committed(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_completed_with_unobservable_output(model="gpt-5-codex")
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200
    assert chain_store.record_count() == 0


@pytest.mark.asyncio
async def test_explicit_empty_completed_output_never_creates_input_only_baseline(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()

    async def stream(**kwargs):
        upstream.calls.append(kwargs)
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp-empty-output",
                "model": "gpt-5-codex",
                "status": "completed",
                "output": [],
            },
        }

    upstream.stream = stream
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["output"] == []
    assert chain_store.record_count() == 0


@pytest.mark.asyncio
async def test_plain_string_input_is_losslessly_normalized_to_a_user_message(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": "hello there"},
        )
    assert resp.status_code == 200
    assert upstream.calls[0]["payload"]["input"] == [{"role": "user", "content": "hello there"}]


@pytest.mark.asyncio
async def test_store_false_and_background_false_are_accepted_and_store_forced_false_upstream(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-5-codex",
                "input": [{"role": "user", "content": "hi"}],
                "store": False,
                "background": False,
            },
        )
    assert resp.status_code == 200
    payload = upstream.calls[0]["payload"]
    assert payload["store"] is False
    assert "background" not in payload


@pytest.mark.asyncio
async def test_store_true_is_still_rejected_not_silently_downgraded(tmp_path):
    accounts = _setup_one_account(tmp_path)
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=ScriptedUpstream(), api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "m", "input": [], "store": True},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_conversation_and_previous_response_id_are_still_rejected(tmp_path):
    accounts = _setup_one_account(tmp_path)
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=ScriptedUpstream(), api_key=API_KEY)

    async with _client(app) as client:
        resp_conv = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "m", "input": [], "conversation": "conv_1"},
        )
        resp_prev = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "m", "input": [], "previous_response_id": "resp_1"},
        )
    assert resp_conv.status_code == 400
    assert resp_prev.status_code == 400


def test_extract_config_includes_model_effective_fields():
    cfg = _extract_config(
        {
            "model": "gpt-5-codex",
            "temperature": 0.2,
            "top_p": 0.9,
            "max_output_tokens": 512,
            "truncation": "auto",
            "include": ["reasoning.encrypted_content"],
            "service_tier": "default",
            "input": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )
    assert cfg["temperature"] == 0.2
    assert cfg["top_p"] == 0.9
    assert cfg["max_output_tokens"] == 512
    assert cfg["truncation"] == "auto"
    assert cfg["include"] == ["reasoning.encrypted_content"]
    assert cfg["service_tier"] == "default"
    assert "input" not in cfg
    assert "stream" not in cfg


def test_affinity_misses_when_only_temperature_changes():
    chain_store = ChainStore()
    input_items = [{"role": "user", "content": "hi"}]
    output_items = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]}]
    cfg_a = _extract_config({"model": "m", "temperature": 0.2})

    match = chain_store.find_match(input_items, cfg_a, {"acct-1"})
    chain_store.commit(
        chain_id=match.chain_id,
        prefix_hashes=match.prefix_hashes,
        input_length=len(input_items),
        output_items=output_items,
        cfg=cfg_a,
        account_ref="acct-1",
    )
    extended_input = input_items + output_items + [{"role": "user", "content": "more"}]

    same_cfg_match = chain_store.find_match(extended_input, cfg_a, {"acct-1"})
    assert same_cfg_match.account_ref == "acct-1"

    cfg_b = _extract_config({"model": "m", "temperature": 0.9})
    diff_cfg_match = chain_store.find_match(extended_input, cfg_b, {"acct-1"})
    assert diff_cfg_match.account_ref is None


@pytest.mark.asyncio
async def test_streaming_transport_error_before_any_bytes_returns_clear_error_status(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_transport_error()
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        async with client.stream(
            "POST",
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}], "stream": True},
        ) as resp:
            assert resp.status_code == 502
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
    assert b"simulated connection reset" in body
    assert chain_store.record_count() == 0


@pytest.mark.asyncio
async def test_streaming_late_error_after_bytes_stays_a_stream_event_not_a_status_change(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_partial_then_fail(
        model="gpt-5-codex",
        partial_item={"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "starting..."}]},
    )
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        async with client.stream(
            "POST",
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            raw = b""
            async for chunk in resp.aiter_bytes():
                raw += chunk
    text = raw.decode()
    event_types = [line[len("event: ") :] for line in text.splitlines() if line.startswith("event: ")]
    assert "response.output_item.added" in event_types
    assert "error" in event_types
    assert chain_store.record_count() == 0
    assert len(upstream.calls) == 1


@pytest.mark.asyncio
async def test_streaming_eof_without_terminal_is_an_error_and_never_commits(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()

    async def stream(**kwargs):
        upstream.calls.append(kwargs)
        yield {"type": "response.created", "response": {"id": "r-eof", "status": "in_progress"}}

    upstream.stream = stream
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        async with client.stream(
            "POST",
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "m", "input": [{"role": "user", "content": "hi"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            body = b"".join([chunk async for chunk in resp.aiter_bytes()])
    assert b"without a terminal event" in body
    assert chain_store.record_count() == 0


@pytest.mark.asyncio
async def test_slow_token_refresh_does_not_serialize_concurrent_requests(tmp_path, monkeypatch):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex", output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]}]
    )
    upstream.queue_success(
        model="gpt-5-codex", output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "b"}]}]
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    def slow_get_access_token(self):
        time.sleep(0.3)
        return "at-1"

    monkeypatch.setattr("codex_pool.server.CodexTokenManager.get_access_token", slow_get_access_token)

    async def make_request():
        async with _client(app) as client:
            return await client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
            )

    start = time.monotonic()
    results = await asyncio.gather(make_request(), make_request())
    elapsed = time.monotonic() - start

    assert all(r.status_code == 200 for r in results)
    assert elapsed < 0.55


# --- Native Codex cache-affinity identity parity -----------------------------


def test_with_encrypted_reasoning_include_preserves_caller_order_and_dedups():
    assert _with_encrypted_reasoning_include({"model": "m"})["include"] == ["reasoning.encrypted_content"]
    assert _with_encrypted_reasoning_include({"include": ["foo"]})["include"] == ["foo", "reasoning.encrypted_content"]
    preordered = {"include": ["foo", "reasoning.encrypted_content", "bar"]}
    assert _with_encrypted_reasoning_include(preordered)["include"] == ["foo", "reasoning.encrypted_content", "bar"]
    assert _with_encrypted_reasoning_include({"include": "reasoning.encrypted_content"})["include"] == [
        "reasoning.encrypted_content"
    ]


def test_effective_include_default_is_visible_to_config_hash_extraction():
    # The default must be applied before `_extract_config` so affinity/config
    # hashing keys off the same `include` list actually forwarded upstream.
    effective = _with_encrypted_reasoning_include({"model": "m", "input": []})
    cfg = _extract_config(effective)
    assert cfg["include"] == ["reasoning.encrypted_content"]


@pytest.mark.asyncio
async def test_encrypted_reasoning_include_default_forwarded_and_preserves_caller_include(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-5-codex",
                "input": [{"role": "user", "content": "hello"}],
                "include": ["file_search_call.results"],
            },
        )
    assert resp.status_code == 200
    assert upstream.calls[0]["payload"]["include"] == ["file_search_call.results", "reasoning.encrypted_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_headers,body_extra,expect_from",
    [
        ({}, {}, "chain_id"),
        ({"session_id": "hdr-session-1"}, {}, "session_header"),
        ({"thread_id": "hdr-thread-1"}, {}, "thread_header"),
        ({"session_id": "hdr-session-2", "thread_id": "hdr-thread-2"}, {}, "session_header"),
        (
            {"session_id": "hdr-session-3", "thread_id": "hdr-thread-3"},
            {"prompt_cache_key": "explicit-key"},
            "prompt_cache_key",
        ),
        # No anchor header: a body-only prompt_cache_key (e.g. a generic SDK's
        # shared/model-global key) is NOT proof of per-caller identity and
        # must not be promoted — the chain_id fallback governs instead.
        ({}, {"prompt_cache_key": "shared-model-wide-key"}, "chain_id"),
    ],
    ids=[
        "headerless_chain_id",
        "session_header_only",
        "thread_header_only",
        "session_wins_over_thread",
        "cache_key_wins_over_both_with_anchor",
        "cache_key_only_no_anchor_falls_back_to_chain_id",
    ],
)
async def test_conversation_identity_precedence_table(tmp_path, extra_headers, body_extra, expect_from):
    """Native anchor rule: an explicit session_id/thread_id header must be
    present before prompt_cache_key/session_id/thread_id can be promoted to
    the upstream identity; otherwise the proxy's own stable per-chain id
    governs all three upstream fields (including replacing a body-only
    cache key)."""
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    body = {"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}], **body_extra}
    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", **extra_headers},
            json=body,
        )
    assert resp.status_code == 200
    call = upstream.calls[0]
    identity = call["session_id"]
    # Native semantics: one stable identity, byte-identical across the header
    # pair and the body cache key.
    assert call["thread_id"] == identity
    assert call["payload"]["prompt_cache_key"] == identity

    caller_supplied_values = {"hdr-session-1", "hdr-thread-1", "hdr-session-2", "hdr-thread-2", "explicit-key", "shared-model-wide-key"}
    if expect_from == "chain_id":
        assert identity not in caller_supplied_values
    elif expect_from == "session_header":
        assert identity == extra_headers["session_id"]
    elif expect_from == "thread_header":
        assert identity == extra_headers["thread_id"]
    elif expect_from == "prompt_cache_key":
        assert identity == "explicit-key"


@pytest.mark.asyncio
async def test_key_only_unrelated_callers_sharing_one_cache_key_do_not_collapse(tmp_path):
    """Two unrelated callers sending the SAME body prompt_cache_key with no
    session_id/thread_id anchor header (an ordinary generic-SDK shape, e.g. a
    shared/model-global key) must land on distinct upstream identities and
    distinct affinity chains — the cache key alone is not per-caller proof."""
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]}],
    )
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "b"}]}],
    )
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-5-codex",
                "input": [{"role": "user", "content": "topic A, unrelated to B"}],
                "prompt_cache_key": "shared-model-wide-key",
            },
        )
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-5-codex",
                "input": [{"role": "user", "content": "topic B, unrelated to A"}],
                "prompt_cache_key": "shared-model-wide-key",
            },
        )

    assert upstream.calls[0]["session_id"] != upstream.calls[1]["session_id"]
    assert upstream.calls[0]["payload"]["prompt_cache_key"] != "shared-model-wide-key"
    assert upstream.calls[1]["payload"]["prompt_cache_key"] != "shared-model-wide-key"
    assert chain_store.record_count() == 2


@pytest.mark.asyncio
async def test_key_only_continuation_still_reuses_chain_identity(tmp_path):
    """A key-only caller (no anchor header) on a genuine content continuation
    still gets a stable, reused upstream identity across turns — the
    headerless chain_id fallback, not the caller's shared cache key."""
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    first_output = [{
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }]
    second_output = [{
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "second reply", "annotations": []}],
    }]
    upstream.queue_success(model="gpt-5-codex", output=first_output)
    upstream.queue_success(model="gpt-5-codex", output=second_output)
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-5-codex",
                "input": [{"role": "user", "content": "hello"}],
                "prompt_cache_key": "shared-model-wide-key",
            },
        )
        second_input = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "continue"},
        ]
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "gpt-5-codex",
                "input": second_input,
                "prompt_cache_key": "shared-model-wide-key",
            },
        )

    assert chain_store.record_count() == 1
    assert upstream.calls[0]["session_id"] == upstream.calls[1]["session_id"]
    assert upstream.calls[0]["payload"]["prompt_cache_key"] == upstream.calls[1]["payload"]["prompt_cache_key"]
    assert upstream.calls[0]["payload"]["prompt_cache_key"] != "shared-model-wide-key"


@pytest.mark.asyncio
async def test_headerless_client_gets_stable_chain_identity_reused_on_continuation(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    first_output = [{
        "type": "message",
        "id": "msg_0",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }]
    second_output = [{
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "second reply", "annotations": []}],
    }]
    upstream.queue_success(model="gpt-5-codex", output=first_output)
    upstream.queue_success(model="gpt-5-codex", output=second_output)
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
        second_input = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "continue"},
        ]
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": second_input},
        )

    # Same conversation (prefix-continuation) -> the owner-generated window
    # identity is reused, not a fresh random id per request.
    assert upstream.calls[0]["session_id"] == upstream.calls[1]["session_id"]
    assert upstream.calls[0]["payload"]["prompt_cache_key"] == upstream.calls[1]["payload"]["prompt_cache_key"]


@pytest.mark.asyncio
async def test_headerless_unrelated_conversations_get_distinct_chain_identity(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]}],
    )
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "b"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "topic A, unrelated to B"}]},
        )
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "topic B, unrelated to A"}]},
        )

    # Distinct conversations must never collapse onto one shared/model-global
    # cache identity.
    assert upstream.calls[0]["session_id"] != upstream.calls[1]["session_id"]


@pytest.mark.asyncio
async def test_scheduling_ignores_caller_session_headers_uses_content_only(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    first_output = [{
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }]
    second_output = [{
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "second reply", "annotations": []}],
    }]
    upstream.queue_success(model="gpt-5-codex", output=first_output)
    upstream.queue_success(model="gpt-5-codex", output=second_output)
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "session_id": "alpha"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
        second_input = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first reply"},
            {"role": "user", "content": "continue"},
        ]
        # A DIFFERENT caller-supplied session header on the content-prefix
        # continuation must not break affinity: scheduling is content-only.
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "session_id": "beta"},
            json={"model": "gpt-5-codex", "input": second_input},
        )

    assert chain_store.record_count() == 1
    assert upstream.calls[0]["account_id"] == upstream.calls[1]["account_id"] == "acct-1"
    # The upstream identity still follows the (changed) caller header, proving
    # headers steer only upstream metadata, never routing.
    assert upstream.calls[0]["session_id"] == "alpha"
    assert upstream.calls[1]["session_id"] == "beta"


@pytest.mark.asyncio
async def test_distinct_content_sharing_a_caller_session_header_is_not_merged(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]}],
    )
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "b"}]}],
    )
    chain_store = ChainStore()
    app = create_app(accounts=accounts, chain_store=chain_store, upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "session_id": "shared"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "topic A, unrelated to B"}]},
        )
        await client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}", "session_id": "shared"},
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "topic B, unrelated to A"}]},
        )

    # A caller reusing the same session header across unrelated content must
    # not merge them into one affinity baseline: content, not the header, is
    # scheduling authority.
    assert chain_store.record_count() == 2


@pytest.mark.asyncio
async def test_caller_supplied_account_id_and_cookie_headers_are_not_forwarded(tmp_path):
    accounts = _setup_one_account(tmp_path)
    upstream = ScriptedUpstream()
    upstream.queue_success(
        model="gpt-5-codex",
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}],
    )
    app = create_app(accounts=accounts, chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/responses",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "ChatGPT-Account-ID": "attacker-acct",
                "Cookie": "session=evil",
            },
            json={"model": "gpt-5-codex", "input": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200
    # account_id passed to Upstream is only ever the server-owned token
    # manager value, never anything read from the caller's own headers.
    assert upstream.calls[0]["account_id"] == "acct-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_include", [False, 1, {}])
async def test_invalid_include_type_is_rejected_before_upstream(tmp_path, invalid_include):
    upstream = ScriptedUpstream()
    app = create_app(accounts=_setup_one_account(tmp_path), chain_store=ChainStore(), upstream=upstream, api_key=API_KEY)
    async with _client(app) as client:
        response = await client.post("/v1/responses", headers={"Authorization": f"Bearer {API_KEY}"}, json={"model": "m", "input": [], "include": invalid_include})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert upstream.calls == []
