from codex_pool.chain import ChainStore
from codex_pool.hashing import rolling_hashes

CFG = {"model": "gpt-5-codex", "instructions": "be helpful", "tools": None}


def _commit(store, *, chain_id, input_items, output_items, account_ref, cfg=CFG):
    prefix = rolling_hashes(input_items)
    store.commit(
        chain_id=chain_id,
        prefix_hashes=prefix,
        input_length=len(input_items),
        output_items=output_items,
        cfg=cfg,
        account_ref=account_ref,
    )


def test_sequential_success_keeps_exactly_one_record_per_chain():
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]

    match = store.find_match(u1, CFG, {"acct"})
    assert match.account_ref is None  # fresh chain
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")
    assert store.record_count() == 1

    u2 = u1 + o1 + [{"role": "user", "content": "again"}]
    o2 = [{"type": "message", "content": [{"type": "output_text", "text": "world"}]}]
    match2 = store.find_match(u2, CFG, {"acct"})
    assert match2.account_ref == "acct"
    assert match2.chain_id == match.chain_id
    _commit(store, chain_id=match2.chain_id, input_items=u2, output_items=o2, account_ref="acct")
    assert store.record_count() == 1  # still one — old record replaced, not appended


def test_actual_responses_message_and_plain_assistant_replay_replace_one_record():
    store = ChainStore()
    first_input = [{"role": "user", "content": "hello"}]
    first_output = [{
        "type": "message",
        "id": "msg_0",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first reply", "annotations": []}],
    }]
    first = store.find_match(first_input, CFG, {"acct"})
    _commit(store, chain_id=first.chain_id, input_items=first_input, output_items=first_output, account_ref="acct")

    second_input = first_input + [{"role": "assistant", "content": "first reply"}, {"role": "user", "content": "continue"}]
    second = store.find_match(second_input, CFG, {"acct"})
    assert second.account_ref == "acct"
    assert second.chain_id == first.chain_id
    _commit(store, chain_id=second.chain_id, input_items=second_input, output_items=first_output, account_ref="acct")
    assert store.record_count() == 1


def test_shared_system_prefix_alone_does_not_match():
    store = ChainStore()
    u1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    match = store.find_match(u1, CFG, {"acct"})
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")

    # Same leading system message, but a divergent conversation after it.
    u_other = [{"role": "system", "content": "sys"}, {"role": "user", "content": "totally different"}]
    match_other = store.find_match(u_other, CFG, {"acct"})
    assert match_other.account_ref is None


def test_config_change_breaks_affinity():
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    match = store.find_match(u1, CFG, {"acct"})
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")

    u2 = u1 + o1 + [{"role": "user", "content": "again"}]
    other_cfg = {**CFG, "model": "different-model"}
    match2 = store.find_match(u2, other_cfg, {"acct"})
    assert match2.account_ref is None


def test_account_no_longer_eligible_forces_reselection():
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    match = store.find_match(u1, CFG, {"acct"})
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")

    u2 = u1 + o1 + [{"role": "user", "content": "again"}]
    match2 = store.find_match(u2, CFG, {"other_acct"})  # "acct" no longer eligible
    assert match2.account_ref is None


def test_failure_does_not_advance_baseline():
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    match = store.find_match(u1, CFG, {"acct"})
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")

    # A second, failed turn never calls commit() at all — simulate by just
    # re-checking the match: the old record must still be intact.
    u2 = u1 + o1 + [{"role": "user", "content": "again"}]
    match2 = store.find_match(u2, CFG, {"acct"})
    assert match2.account_ref == "acct"
    assert match2.chain_id == match.chain_id
    assert store.record_count() == 1


def test_late_history_after_a_newer_commit_misses():
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    match = store.find_match(u1, CFG, {"acct"})
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")

    u2 = u1 + o1 + [{"role": "user", "content": "next"}]
    o2 = [{"type": "message", "content": [{"type": "output_text", "text": "next reply"}]}]
    match2 = store.find_match(u2, CFG, {"acct"})
    _commit(store, chain_id=match2.chain_id, input_items=u2, output_items=o2, account_ref="acct")

    # A request still built on the now-superseded u1+o1 history no longer
    # matches; it falls back to ordinary load balancing (no branch kept).
    stale_next = u1 + o1 + [{"role": "user", "content": "late arrival"}]
    stale_match = store.find_match(stale_next, CFG, {"acct"})
    assert stale_match.account_ref is None
    assert store.record_count() == 1  # the stale history was never retained as a branch


def test_competing_updates_to_the_same_matched_slot_leave_one_record():
    # Two requests both match the same current record and race to extend
    # it. There is no fork preservation: whichever commit lands last simply
    # becomes the new current record for that chain_id.
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    match = store.find_match(u1, CFG, {"acct"})
    _commit(store, chain_id=match.chain_id, input_items=u1, output_items=o1, account_ref="acct")

    u2 = u1 + o1 + [{"role": "user", "content": "concurrent turn"}]
    match_a = store.find_match(u2, CFG, {"acct"})
    match_b = store.find_match(u2, CFG, {"acct"})
    assert match_a.chain_id == match_b.chain_id == match.chain_id

    out_a = [{"type": "message", "content": [{"type": "output_text", "text": "A's answer"}]}]
    out_b = [{"type": "message", "content": [{"type": "output_text", "text": "B's answer"}]}]
    _commit(store, chain_id=match_a.chain_id, input_items=u2, output_items=out_a, account_ref="acct")
    _commit(store, chain_id=match_b.chain_id, input_items=u2, output_items=out_b, account_ref="acct")

    assert store.record_count() == 1  # one current record, not two competing branches

    # The next request only ever sees B's content (the last completed update).
    follow_up = u2 + out_b + [{"role": "user", "content": "continue"}]
    follow_match = store.find_match(follow_up, CFG, {"acct"})
    assert follow_match.account_ref == "acct"
    assert follow_match.chain_id == match.chain_id
