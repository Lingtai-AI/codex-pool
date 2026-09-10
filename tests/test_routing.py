from pathlib import Path

import pytest

from codex_pool.accounts import Account
from codex_pool.chain import ChainStore
from codex_pool.routing import NoEligibleAccountError, select_account, weighted_choice
from fakes import write_auth_fixture

CFG = {"model": "gpt-5-codex", "instructions": None, "tools": None}


def _account(ref, tmp_path, *, enabled=True, weight=1, authenticated=True):
    auth = tmp_path / f"{ref}.json"
    if authenticated:
        write_auth_fixture(auth)
    else:
        auth.write_text("{}")  # no refresh_token -> not authenticated
    return Account(ref=ref, auth_path=str(auth), enabled=enabled, weight=weight)


def test_weighted_choice_is_proportional_and_reproducible(tmp_path):
    a = _account("a", tmp_path, weight=1)
    b = _account("b", tmp_path, weight=3)
    counter = iter([0, 1, 2, 3, 0, 1, 2, 3])  # total weight = 4
    picks = [weighted_choice([a, b], randbelow=lambda n: next(counter) % n) for _ in range(8)]
    assert picks.count("a") == 2  # r in {0} maps to a, per 4-wide cycle -> a picked when r==0
    assert picks.count("b") == 6


def test_weighted_choice_raises_when_no_accounts():
    with pytest.raises(NoEligibleAccountError):
        weighted_choice([])


def test_no_match_load_balances_among_eligible(tmp_path):
    a = _account("a", tmp_path, weight=1)
    b = _account("b", tmp_path, weight=1, enabled=False)  # disabled -> ineligible
    store = ChainStore()
    decision = select_account(accounts=[a, b], input_items=[{"role": "user", "content": "hi"}], cfg=CFG, chain_store=store, randbelow=lambda n: 0)
    assert decision.account_ref == "a"
    assert decision.matched is False


def test_unauthenticated_account_is_explicitly_ineligible(tmp_path):
    a = _account("a", tmp_path, authenticated=False)
    store = ChainStore()
    with pytest.raises(NoEligibleAccountError):
        select_account(accounts=[a], input_items=[{"role": "user", "content": "hi"}], cfg=CFG, chain_store=store)


def test_malformed_json_auth_account_is_skipped_and_valid_account_selected(tmp_path):
    bad_auth = tmp_path / "bad.json"
    bad_auth.write_text("[]", encoding="utf-8")
    bad = Account(ref="bad", auth_path=str(bad_auth), weight=100)
    good = _account("good", tmp_path)
    decision = select_account(
        accounts=[bad, good],
        input_items=[{"role": "user", "content": "hi"}],
        cfg=CFG,
        chain_store=ChainStore(),
        randbelow=lambda n: n - 1,
    )
    assert decision.account_ref == "good"


def test_full_prefix_match_sticks_to_same_account(tmp_path):
    a = _account("a", tmp_path, weight=1)
    b = _account("b", tmp_path, weight=100)  # heavily favored by LB, but must NOT be picked on a match
    store = ChainStore()

    u1 = [{"role": "user", "content": "hi"}]
    first = select_account(accounts=[a, b], input_items=u1, cfg=CFG, chain_store=store, randbelow=lambda n: n - 1)
    assert first.matched is False

    o1 = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    store.commit(
        chain_id=first.chain_id,
        prefix_hashes=first.prefix_hashes,
        input_length=len(u1),
        output_items=o1,
        cfg=CFG,
        account_ref=first.account_ref,
    )

    u2 = u1 + o1 + [{"role": "user", "content": "again"}]
    second = select_account(accounts=[a, b], input_items=u2, cfg=CFG, chain_store=store, randbelow=lambda n: n - 1)
    assert second.matched is True
    assert second.account_ref == first.account_ref


def test_token_refresh_is_not_an_account_swap(tmp_path):
    # An account nearing/past expiry is still "authenticated" (has a refresh
    # token) and stays eligible; refreshing it does not change which account
    # is selected relative to a fresh one with the same weight.
    a = _account("a", tmp_path, weight=1)
    write_auth_fixture(Path(a.auth_path), expires_in=-10)
    store = ChainStore()
    decision = select_account(accounts=[a], input_items=[{"role": "user", "content": "hi"}], cfg=CFG, chain_store=store)
    assert decision.account_ref == "a"  # still selectable; expiry alone never excludes it


def test_unknown_quota_does_not_exclude_account(tmp_path):
    a = _account("a", tmp_path, weight=1)
    store = ChainStore()
    decision = select_account(accounts=[a], input_items=[{"role": "user", "content": "hi"}], cfg=CFG, chain_store=store)
    assert decision.account_ref == "a"


def test_known_exhaustion_excludes_account_but_other_account_remains(tmp_path):
    exhausted = _account("exhausted", tmp_path, weight=1)
    exhausted.quota_exhausted = True
    available = _account("available", tmp_path, weight=1)
    store = ChainStore()
    decision = select_account(
        accounts=[exhausted, available],
        input_items=[{"role": "user", "content": "hi"}],
        cfg=CFG,
        chain_store=store,
        randbelow=lambda n: 0,
    )
    assert decision.account_ref == "available"
