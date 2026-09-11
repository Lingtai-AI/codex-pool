"""Bounded in-memory content-chain affinity table.

At most one current record per inferred conversation chain: digest, length,
model-effective config digest, and the account that produced it. A request
whose input full-prefix-matches a stored record's content gets routed back
to that record's account (affinity); a successful completion then replaces
that one record with the new baseline. There is no branch/lineage concept:
if two requests race to extend the same record, whichever commits last
simply becomes the new current record — the other's output is not
preserved or merged. A request built on older, already-superseded history
just misses and falls back to ordinary weighted load balancing. Failed,
partial, or cancelled turns never call ``commit()``, so they never replace
the current record.

Each chain's ``chain_id`` is the pool-owned session identity: generated once
per new inferred chain from time plus CSPRNG bits (never from content), then
carried unchanged by every sticky continuation. The server sends it upstream
as the conversation's cache/session label.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass

from .hashing import config_hash as compute_config_hash
from .hashing import extend_hash, rolling_hashes

# Default number of most-recently-committed session records retained;
# `codex-pool serve` overrides it from CODEX_POOL_MAX_SESSIONS at startup.
DEFAULT_MAX_SESSIONS = 100000


def new_session_id() -> str:
    """Return a fresh session label independent of conversation content.

    UUIDv7 layout: 48-bit Unix milliseconds from ``time.time_ns()`` plus 74
    bits from ``secrets``, in the same 36-character UUID text shape the
    previous random chain ids used. Uniqueness is probabilistic (random bits),
    not guaranteed by the clock; ``ChainStore`` additionally refuses to reuse
    an id it currently retains.
    """
    ms = time.time_ns() // 1_000_000
    rand = secrets.randbits(74)
    value = (
        (ms & 0xFFFF_FFFF_FFFF) << 80
        | 0x7 << 76  # version 7
        | (rand >> 62) << 64
        | 0b10 << 62  # RFC 4122 variant
        | (rand & ((1 << 62) - 1))
    )
    return str(uuid.UUID(int=value))


@dataclass(frozen=True)
class Baseline:
    chain_id: str
    content_hash: str
    length: int
    config_hash: str
    account_ref: str


@dataclass(frozen=True)
class MatchResult:
    """A full-prefix affinity match, or the absence of one (a fresh chain)."""

    chain_id: str
    account_ref: str | None  # None when no match — caller load-balances
    prefix_hashes: list[str]  # rolling hashes over the candidate input items


class ChainStore:
    """Thread-safe; a single process-local table (max_records bounded)."""

    def __init__(self, max_records: int = DEFAULT_MAX_SESSIONS) -> None:
        self._records: dict[str, Baseline] = {}
        self._lock = threading.Lock()
        self._max_records = max_records

    def find_match(self, input_items: list, cfg: dict, eligible_refs: set[str]) -> MatchResult:
        """Find the longest full-prefix baseline match still eligible.

        Only ``input_items`` need hashing (O(n)); comparison against every
        stored record is then O(1) per record.
        """
        cfg_hash = compute_config_hash(cfg)
        prefix_hashes = rolling_hashes(input_items)
        n = len(input_items)

        with self._lock:
            candidates = [
                rec
                for rec in self._records.values()
                if rec.config_hash == cfg_hash
                and rec.length <= n
                and rec.account_ref in eligible_refs
                and prefix_hashes[rec.length] == rec.content_hash
            ]
            if not candidates:
                return MatchResult(chain_id=self._unused_id_locked(), account_ref=None, prefix_hashes=prefix_hashes)

        # Longest match wins; deterministic tie-break by chain_id.
        best = sorted(candidates, key=lambda r: (-r.length, r.chain_id))[0]
        return MatchResult(chain_id=best.chain_id, account_ref=best.account_ref, prefix_hashes=prefix_hashes)

    def commit(
        self,
        *,
        chain_id: str,
        prefix_hashes: list[str],
        input_length: int,
        output_items: list,
        cfg: dict,
        account_ref: str,
        fresh: bool = False,
    ) -> None:
        """Replace the current record for ``chain_id`` with this completion.

        No CAS, no versioning: if another commit lands on the same
        ``chain_id`` concurrently, the last write simply wins as the new
        current record. Callers must not call this for failed, partial, or
        cancelled turns.

        ``fresh=True`` marks the first commit of a new (unmatched) chain. If
        its id became retained by another chain after ``find_match`` issued
        it, a new unused id is stored instead of overwriting that session.
        """
        cfg_hash = compute_config_hash(cfg)
        full_hash = extend_hash(prefix_hashes[input_length], output_items)
        full_length = input_length + len(output_items)

        with self._lock:
            if fresh and chain_id in self._records:
                chain_id = self._unused_id_locked()
            self._records.pop(chain_id, None)  # re-insert at MRU end
            self._records[chain_id] = Baseline(
                chain_id=chain_id,
                content_hash=full_hash,
                length=full_length,
                config_hash=cfg_hash,
                account_ref=account_ref,
            )
            self._evict_if_needed()

    def _unused_id_locked(self) -> str:
        # Caller holds the lock. Regenerate on the (improbable) collision
        # with a retained session so an existing record is never overwritten.
        chain_id = new_session_id()
        while chain_id in self._records:
            chain_id = new_session_id()
        return chain_id

    def _evict_if_needed(self) -> None:
        # Bounded table: drop the least-recently-committed record on overflow
        # (commit re-inserts at the end). Restart or eviction transparently
        # loses affinity per contract (not a bug).
        while len(self._records) > self._max_records:
            oldest_id = next(iter(self._records))
            del self._records[oldest_id]

    def record_count(self) -> int:
        with self._lock:
            return len(self._records)
