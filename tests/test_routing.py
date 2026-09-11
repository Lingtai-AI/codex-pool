from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subs_pool.modules.codex.accounts import Account
from subs_pool.modules.codex.accounts import AccountStore
from subs_pool.modules.codex.chain import ChainStore
from subs_pool.modules.codex.quota_store import QuotaStore, iso
from subs_pool.modules.codex.routing import NoEligibleAccountError, select_account, weighted_choice
from fakes import write_auth_fixture

CFG = {"model": "gpt-5-codex", "instructions": None, "tools": None}


def _account(ref, tmp_path, *, enabled=True, weight=1, authenticated=True):
    auth = tmp_path / f"{ref}.json"
    if authenticated:
        write_auth_fixture(auth)
    else:
        auth.write_text("{}")
    return Account(ref=ref, auth_path=str(auth), enabled=enabled, weight=weight)


def _seed(accounts, root: Path, *, used: float = 10.0) -> QuotaStore:
    config = AccountStore(root / "pool.json")
    for account in accounts:
        stored = config.import_account(account.ref, account.auth_path, weight=account.weight)
        if stored.enabled != account.enabled:
            stored = config.set_enabled(account.ref, account.enabled)
        account.quota_epoch = stored.quota_epoch
    now = datetime.now(timezone.utc)
    store = QuotaStore(root)
    eligible = [account for account in accounts if account.enabled and account.local_authenticated(root=root)]
    store.claim(eligible, attempt_id="fixture", owner_id="test", deadline_at=now + timedelta(seconds=20))
    for account in eligible:
        store.commit_success(
            account,
            attempt_id="fixture",
            sample={
                "source_at": iso(now), "checked_at": iso(now), "fresh_until": iso(now + timedelta(seconds=60)),
                "allowed": True, "limit_reached": False,
                "primary": {"used_percent": used, "remaining_percent": 100 - used, "reset_at": None, "window_seconds": None},
                "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
            },
        )
    return store


def test_weighted_choice_is_proportional_and_reproducible(tmp_path):
    a = _account("a", tmp_path, weight=1)
    b = _account("b", tmp_path, weight=3)
    counter = iter([0, 1, 2, 3, 0, 1, 2, 3])
    picks = [weighted_choice([a, b], randbelow=lambda n: next(counter) % n) for _ in range(8)]
    assert picks.count("a") == 2
    assert picks.count("b") == 6


def test_weighted_choice_raises_when_no_accounts():
    with pytest.raises(NoEligibleAccountError):
        weighted_choice([])


def test_routing_requires_fresh_sidecar_and_skips_disabled(tmp_path):
    a = _account("a", tmp_path)
    b = _account("b", tmp_path, enabled=False, weight=2)
    quota = _seed([a, b], tmp_path)
    decision = select_account(
        accounts=[a, b], input_items=[{"role": "user", "content": "hi"}], cfg=CFG,
        chain_store=ChainStore(), quota_store=quota, snapshot=quota.read(), randbelow=lambda n: 0,
    )
    assert decision.account_ref == "a"


def test_unknown_quota_is_fail_closed(tmp_path):
    a = _account("a", tmp_path)
    with pytest.raises(NoEligibleAccountError):
        select_account(accounts=[a], input_items=[{"role": "user", "content": "hi"}], cfg=CFG, chain_store=ChainStore(), quota_store=QuotaStore(tmp_path))


def test_malformed_auth_account_is_skipped_and_valid_account_selected(tmp_path):
    bad_auth = tmp_path / "bad.json"
    bad_auth.write_text("[]", encoding="utf-8")
    bad = Account(ref="bad", auth_path=str(bad_auth), weight=100)
    good = _account("good", tmp_path)
    quota = _seed([bad, good], tmp_path)
    decision = select_account(
        accounts=[bad, good], input_items=[{"role": "user", "content": "hi"}], cfg=CFG,
        chain_store=ChainStore(), quota_store=quota, snapshot=quota.read(), randbelow=lambda n: n - 1,
    )
    assert decision.account_ref == "good"


def test_full_prefix_match_sticks_to_same_current_account(tmp_path):
    a = _account("a", tmp_path, weight=1)
    b = _account("b", tmp_path, weight=100)
    quota = _seed([a, b], tmp_path)
    store = ChainStore()
    u1 = [{"role": "user", "content": "hi"}]
    first = select_account(accounts=[a, b], input_items=u1, cfg=CFG, chain_store=store, quota_store=quota, snapshot=quota.read(), randbelow=lambda n: n - 1)
    output = [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]
    store.commit(chain_id=first.chain_id, prefix_hashes=first.prefix_hashes, input_length=len(u1), output_items=output, cfg=CFG, account_ref=first.account_ref)
    second = select_account(accounts=[a, b], input_items=u1 + output + [{"role": "user", "content": "again"}], cfg=CFG, chain_store=store, quota_store=quota, snapshot=quota.read(), randbelow=lambda n: n - 1)
    assert second.matched is True
    assert second.account_ref == first.account_ref


def test_bound_account_stale_or_exhausted_does_not_fail_over(tmp_path):
    a = _account("a", tmp_path)
    b = _account("b", tmp_path)
    quota = _seed([a, b], tmp_path)
    store = ChainStore()
    first_input = [{"role": "user", "content": "hi"}]
    first = select_account(accounts=[a, b], input_items=first_input, cfg=CFG, chain_store=store, quota_store=quota, snapshot=quota.read(), randbelow=lambda n: 0)
    output = [{"type": "message", "content": [{"type": "output_text", "text": "x"}]}]
    store.commit(chain_id=first.chain_id, prefix_hashes=first.prefix_hashes, input_length=1, output_items=output, cfg=CFG, account_ref=first.account_ref)
    now = datetime.now(timezone.utc)
    quota.claim([a], attempt_id="exhaust", owner_id="test", deadline_at=now + timedelta(seconds=20))
    quota.commit_success(a, attempt_id="exhaust", sample={
        "source_at": iso(now), "checked_at": iso(now), "fresh_until": iso(now + timedelta(seconds=60)),
        "allowed": False, "limit_reached": True,
        "primary": {"used_percent": 100, "remaining_percent": 0, "reset_at": None, "window_seconds": None},
        "secondary": {"used_percent": None, "remaining_percent": None, "reset_at": None, "window_seconds": None},
    })
    with pytest.raises(NoEligibleAccountError):
        select_account(accounts=[a, b], input_items=first_input + output + [{"role": "user", "content": "again"}], cfg=CFG, chain_store=store, quota_store=quota, snapshot=quota.read())
