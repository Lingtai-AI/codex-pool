"""Account/pool state owner.

Persists non-secret account references (a ref name and a pointer to an auth
file) plus pool membership (enabled/weight) under the data-root resolved by
:mod:`codex_pool.home`. A small optional quota-exhaustion observation is also
stored so a real, explicit 100% quota reading can make an account ineligible;
unknown observations never do. Tokens remain in the referenced auth file and
are never returned by this module.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock

from .home import pool_state_path


class AccountError(Exception):
    """Raised for account/pool state problems (unknown ref, bad weight, ...)."""


@dataclass
class Account:
    ref: str
    auth_path: str
    enabled: bool = True
    weight: int = 1
    imported_at: float = field(default_factory=time.time)
    # None means no known quota observation; True means an explicit exhausted
    # reading, and False means a known non-exhausted reading. It is internal
    # pool state and intentionally omitted from the public status object.
    quota_exhausted: bool | None = None

    def to_status_dict(self) -> dict:
        """Non-secret view; quota readings remain on the separate quota surface."""
        return {
            "ref": self.ref,
            "enabled": self.enabled,
            "weight": self.weight,
            "auth_present": os.path.isfile(os.path.expanduser(self.auth_path)),
            "quota": "unknown",
        }


class AccountStore:
    """Loads/saves ``<data-root>/pool.json``; every mutation is atomic + locked."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or pool_state_path()
        self._lock_path = self._path.with_suffix(".json.lock")

    def _load(self) -> dict[str, Account]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise AccountError(f"corrupt pool state file: {self._path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("accounts", {}), dict):
            raise AccountError(f"corrupt pool state file: {self._path}")
        accounts: dict[str, Account] = {}
        for ref, entry in raw.get("accounts", {}).items():
            if not isinstance(ref, str) or not isinstance(entry, dict):
                raise AccountError(f"corrupt pool state file: {self._path}")
            try:
                quota_exhausted = entry.get("quota_exhausted")
                if quota_exhausted is not None and not isinstance(quota_exhausted, bool):
                    quota_exhausted = None
                accounts[ref] = Account(
                    ref=ref,
                    auth_path=entry["auth_path"],
                    enabled=bool(entry.get("enabled", True)),
                    weight=int(entry.get("weight", 1)),
                    imported_at=float(entry.get("imported_at", 0.0)),
                    quota_exhausted=quota_exhausted,
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise AccountError(f"corrupt pool state file: {self._path}") from exc
        return accounts

    def _save(self, accounts: dict[str, Account]) -> None:
        payload = {
            "accounts": {
                ref: {
                    "auth_path": account.auth_path,
                    "enabled": account.enabled,
                    "weight": account.weight,
                    "imported_at": account.imported_at,
                    "quota_exhausted": account.quota_exhausted,
                }
                for ref, account in accounts.items()
            }
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def list(self) -> list[Account]:
        return sorted(self._load().values(), key=lambda account: account.ref)

    def get(self, ref: str) -> Account:
        accounts = self._load()
        try:
            return accounts[ref]
        except KeyError:
            raise AccountError(f"unknown account ref: {ref}") from None

    def import_account(self, ref: str, auth_path: str, *, weight: int = 1) -> Account:
        if not ref or "/" in ref or "\\" in ref or ref in {".", ".."}:
            raise AccountError(f"invalid account ref: {ref!r}")
        if weight <= 0:
            raise AccountError("weight must be a positive integer")
        with FileLock(str(self._lock_path), timeout=30):
            accounts = self._load()
            accounts[ref] = Account(ref=ref, auth_path=auth_path, enabled=True, weight=weight)
            self._save(accounts)
            return accounts[ref]

    def set_enabled(self, ref: str, enabled: bool) -> Account:
        with FileLock(str(self._lock_path), timeout=30):
            accounts = self._load()
            if ref not in accounts:
                raise AccountError(f"unknown account ref: {ref}")
            accounts[ref].enabled = enabled
            self._save(accounts)
            return accounts[ref]

    def set_auth_path(self, ref: str, auth_path: str) -> Account:
        """Re-point an existing account at a new auth file and reset stale quota."""
        with FileLock(str(self._lock_path), timeout=30):
            accounts = self._load()
            if ref not in accounts:
                raise AccountError(f"unknown account ref: {ref}")
            accounts[ref].auth_path = auth_path
            accounts[ref].quota_exhausted = None
            self._save(accounts)
            return accounts[ref]

    def set_quota_exhausted(self, ref: str, exhausted: bool | None) -> Account:
        """Record one explicit quota observation without exposing it in status."""
        if exhausted is not None and not isinstance(exhausted, bool):
            raise AccountError("quota exhaustion must be true, false, or unknown")
        with FileLock(str(self._lock_path), timeout=30):
            accounts = self._load()
            if ref not in accounts:
                raise AccountError(f"unknown account ref: {ref}")
            accounts[ref].quota_exhausted = exhausted
            self._save(accounts)
            return accounts[ref]

    def set_weight(self, ref: str, weight: int) -> Account:
        if weight <= 0:
            raise AccountError("weight must be a positive integer")
        with FileLock(str(self._lock_path), timeout=30):
            accounts = self._load()
            if ref not in accounts:
                raise AccountError(f"unknown account ref: {ref}")
            accounts[ref].weight = weight
            self._save(accounts)
            return accounts[ref]

    def eligible(self) -> list[Account]:
        """Enabled members not known exhausted; unknown quota stays eligible."""
        return [account for account in self.list() if account.enabled and account.quota_exhausted is not True]
