import pytest

from codex_pool.sse import SSEDecoder, decode_sse_stream, encode_event


def test_decode_full_event_in_one_chunk():
    decoder = SSEDecoder()
    raw = encode_event("response.created", {"type": "response.created", "id": "r1"})
    assert decoder.feed(raw) == [{"type": "response.created", "id": "r1"}]


def test_decode_survives_arbitrary_byte_level_chunk_splits():
    raw = encode_event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hello"})
    raw += encode_event("response.completed", {"type": "response.completed", "response": {"status": "completed"}})

    for split_size in (1, 2, 3, 7, 13):
        decoder = SSEDecoder()
        events = []
        for i in range(0, len(raw), split_size):
            events.extend(decoder.feed(raw[i : i + split_size]))
        assert [e["type"] for e in events] == ["response.output_text.delta", "response.completed"]


def test_ignores_done_sentinel_and_comments():
    decoder = SSEDecoder()
    events = decoder.feed(b": keep-alive\ndata: [DONE]\n\nevent: x\ndata: {\"type\": \"x\", \"n\": 1}\n\n")
    assert events == [{"type": "x", "n": 1}]


def test_decode_survives_byte_level_splits_of_multibyte_utf8_content():
    payload = {"type": "response.output_text.delta", "delta": "你好，世界！这是多字节的 emoji 🎉 测试"}
    raw = encode_event("response.output_text.delta", payload)
    raw += encode_event("response.completed", {"type": "response.completed", "response": {"status": "completed"}})

    decoder = SSEDecoder()
    events: list = []
    for i in range(len(raw)):
        events.extend(decoder.feed(raw[i : i + 1]))
    events.extend(decoder.finish())
    assert events == [payload, {"type": "response.completed", "response": {"status": "completed"}}]


def test_decode_handles_crlf_line_endings_split_at_arbitrary_byte_boundaries():
    raw = b"event: x\r\ndata: {\"type\": \"x\", \"n\": 1}\r\n\r\n"
    for split_size in (1, 2, 3, 5):
        decoder = SSEDecoder()
        events: list = []
        for i in range(0, len(raw), split_size):
            events.extend(decoder.feed(raw[i : i + split_size]))
        events.extend(decoder.finish())
        assert events == [{"type": "x", "n": 1}]


def test_decode_handles_crlf_terminator_split_exactly_between_cr_and_lf():
    decoder = SSEDecoder()
    events = decoder.feed(b"event: x\r")
    events += decoder.feed(b"\ndata: {\"type\": \"x\", \"n\": 2}\r")
    events += decoder.feed(b"\n\r\n")
    events += decoder.finish()
    assert events == [{"type": "x", "n": 2}]


def test_decode_handles_bare_cr_line_endings():
    raw = b"event: x\rdata: {\"type\": \"x\", \"n\": 3}\r\r:"
    decoder = SSEDecoder()
    events = decoder.feed(raw)
    events += decoder.finish()
    assert events == [{"type": "x", "n": 3}]


def test_decode_never_yields_a_partial_event_as_success_on_malformed_json():
    decoder = SSEDecoder()
    with pytest.raises(Exception):
        decoder.feed(b"event: x\ndata: {not valid json\n\n")


@pytest.mark.asyncio
async def test_async_decoder_flushes_final_utf8_and_events():
    raw = encode_event("x", {"type": "x", "delta": "尾"})

    async def chunks():
        for byte in raw:
            yield bytes([byte])

    assert [event async for event in decode_sse_stream(chunks())] == [{"type": "x", "delta": "尾"}]


def test_finish_rejects_incomplete_utf8():
    decoder = SSEDecoder()
    decoder.feed(b"data: \xe4")
    with pytest.raises(UnicodeDecodeError):
        decoder.finish()
