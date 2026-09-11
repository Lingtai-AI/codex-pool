from subs_pool.modules.codex.hashing import config_hash, extend_hash, item_hash, rolling_hashes


def test_rolling_hash_is_order_and_content_sensitive():
    items = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    h1 = rolling_hashes(items)
    h2 = rolling_hashes(list(reversed(items)))
    assert h1[-1] != h2[-1]
    assert h1[0] == h2[0]  # empty prefix is identity regardless of content


def test_rolling_hash_prefix_matches_recomputed_hash_of_same_prefix():
    items = [{"a": 1}, {"b": 2}, {"c": 3}]
    full = rolling_hashes(items)
    prefix_only = rolling_hashes(items[:2])
    assert full[2] == prefix_only[2]


def test_extend_hash_equals_full_recompute():
    items = [{"a": 1}, {"b": 2}, {"c": 3}]
    full = rolling_hashes(items)[-1]
    prefix_hash = rolling_hashes(items[:2])[-1]
    extended = extend_hash(prefix_hash, items[2:])
    assert extended == full


def test_item_hash_stable_across_key_order():
    assert item_hash({"a": 1, "b": 2}) == item_hash({"b": 2, "a": 1})


def test_config_hash_distinguishes_model():
    assert config_hash({"model": "a"}) != config_hash({"model": "b"})


def test_plain_assistant_output_envelope_matches_replayed_input_message():
    output = {
        "type": "message",
        "id": "msg_0",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }
    replayed = {"role": "assistant", "content": "first reply"}
    assert item_hash(output) == item_hash(replayed)
    assert rolling_hashes([output]) == rolling_hashes([replayed])


def test_plain_assistant_equivalence_rejects_unknown_or_semantic_fields():
    replayed = {"role": "assistant", "content": "first reply"}
    output = {
        "type": "message",
        "id": "msg_0",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }
    assert item_hash({**replayed, "metadata": {"source": "client"}}) != item_hash(output)
    assert item_hash({**output, "status": "in_progress"}) != item_hash(replayed)
    assert item_hash({**output, "content": [{"type": "output_text", "text": "changed", "annotations": []}]}) != item_hash(replayed)
    assert item_hash({**output, "content": [{"type": "output_text", "text": "first reply", "annotations": [{"url": "x"}]}]}) != item_hash(replayed)
    assert item_hash({**output, "content": [{"type": "refusal", "refusal": "no"}]}) != item_hash(replayed)
    assert item_hash({**output, "role": "user"}) != item_hash(replayed)
    tool_call = {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": "{}"}
    assert item_hash(tool_call) != item_hash({**tool_call, "name": "other"})
    assert item_hash(tool_call) != item_hash({**tool_call, "call_id": "call_2"})
    assert item_hash(tool_call) != item_hash({**tool_call, "arguments": '{"q":"x"}'})
