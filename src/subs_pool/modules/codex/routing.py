"""The two scheduling rules: full-prefix affinity, else weighted load balance.

Rule A — no match: weighted pick among eligible pool accounts.
Rule B — full-prefix match + account still eligible: sticky to that account.

Eligibility here means "in the pool, enabled, and authenticated" (an
account with a missing/invalid auth file is explicitly unavailable — never
excluded merely because its quota is unknown, and a token refresh never
counts as an account swap since it does not change which account is bound).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from .accounts import Account
from .auth_codex import CodexTokenManager
from .chain import ChainStore, MatchResult
from .quota_store import QuotaStore


class NoEligibleAccountError(Exception):
    """No pool member is enabled and authenticated."""


@dataclass(frozen=True)
class RoutingDecision:
    account_ref: str
    chain_id: str
    matched: bool
    prefix_hashes: list


def _is_authenticated(account: Account, *, root: Path | None = None) -> bool:
    try:
        path = account.resolved_auth_path(root or Path.cwd())
        return CodexTokenManager(str(path)).is_authenticated()
    except (FileNotFoundError, OSError, ValueError):
        return False


def eligible_refs(
    accounts: list[Account],
    *,
    quota_store: QuotaStore | None = None,
    snapshot: dict | None = None,
) -> set[str]:
    """Return only accounts with a current, committed quota sample.

    A missing sidecar is intentionally not an eligible state. The optional
    store argument is useful to callers that already hold a consistent
    snapshot; production routing always supplies the shared store.
    """
    if quota_store is None:
        quota_store = QuotaStore()
    return {
        account.ref
        for account in accounts
        if account.enabled
        and _is_authenticated(account, root=quota_store.root)
        and quota_store.is_eligible(account, snapshot=snapshot)
    }


def weighted_choice(accounts: list[Account], *, randbelow=None) -> str:
    """Unbiased weighted draw among eligible accounts, via ``secrets``.

    ``randbelow`` defaults to :func:`secrets.randbelow`; tests inject a
    deterministic stand-in to make weight-proportional selection
    reproducible without weakening the real (cryptographic) draw.

    Static weights only in this pass: dynamic (quota-scaled) weighting needs
    real quota data, which this pass does not implement (see
    IMPLEMENTATION_REPORT.md).
    """
    if not accounts:
        raise NoEligibleAccountError("no enabled, authenticated pool accounts")
    randbelow = randbelow or secrets.randbelow
    total = sum(a.weight for a in accounts)
    r = randbelow(total)
    upto = 0
    for a in accounts:
        upto += a.weight
        if r < upto:
            return a.ref
    return accounts[-1].ref  # unreachable in practice; defensive fallback


def select_account(
    *,
    accounts: list[Account],
    input_items: list,
    cfg: dict,
    chain_store: ChainStore,
    randbelow=None,
    quota_store: QuotaStore | None = None,
    snapshot: dict | None = None,
) -> RoutingDecision:
    elig = eligible_refs(accounts, quota_store=quota_store, snapshot=snapshot)
    if not elig:
        raise NoEligibleAccountError("no current quota-eligible account")

    # Preserve hard continuation affinity: a matching committed chain whose
    # bound account has gone stale/exhausted is a local unavailable result,
    # not permission to rewrite the conversation onto another account.
    bound = chain_store.find_bound(input_items, cfg)
    if bound.account_ref is not None and bound.account_ref not in elig:
        raise NoEligibleAccountError("the bound account has no current quota")

    match: MatchResult = chain_store.find_match(input_items, cfg, elig)
    if match.account_ref is not None:
        return RoutingDecision(
            account_ref=match.account_ref,
            chain_id=match.chain_id,
            matched=True,
            prefix_hashes=match.prefix_hashes,
        )

    eligible_accounts = [a for a in accounts if a.ref in elig]
    chosen = weighted_choice(eligible_accounts, randbelow=randbelow)
    return RoutingDecision(
        account_ref=chosen,
        chain_id=match.chain_id,
        matched=False,
        prefix_hashes=match.prefix_hashes,
    )
