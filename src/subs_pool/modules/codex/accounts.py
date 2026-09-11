"""Codex account configuration and mutation fencing.

The account file is durable configuration only. Transient quota observations
live in :mod:`quota_store`; the legacy ``quota_exhausted`` field is accepted
when reading old files but is never used for routing or written back.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout

from .home import data_home, pool_state_path

LEGACY_QUOTA_EPOCH = "legacy-v1"


class AccountError(Exception):
    """Raised for account/configuration problems."""


def _private_dir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed and os.name != "nt":
        path.chmod(0o700)


def _atomic_json(path: Path, payload: dict) -> None:
    _private_dir(path.parent)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            path.chmod(0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@dataclass
class Account:
    ref: str
    auth_path: str
    enabled: bool = True
    weight: int = 1
    imported_at: float = field(default_factory=time.time)
    quota_epoch: str = LEGACY_QUOTA_EPOCH

    @property
    def id(self) -> str:
        return self.ref

    def resolved_auth_path(self, root: Path) -> Path:
        path = Path(self.auth_path).expanduser()
        return path if path.is_absolute() else root / path

    def to_status_dict(self, *, root: Path | None = None) -> dict:
        root = root or data_home()
        return {
            "ref": self.ref,
            "enabled": self.enabled,
            "weight": self.weight,
            "auth_present": self.resolved_auth_path(root).is_file(),
            "authenticated": self.local_authenticated(root=root),
        }

    def local_authenticated(self, *, root: Path | None = None) -> bool:
        root = root or data_home(create=False)
        try:
            from .auth_codex import CodexTokenManager

            return CodexTokenManager(str(self.resolved_auth_path(root))).is_authenticated()
        except (OSError, ValueError, TypeError):
            return False


class AccountStore:
    """Load and atomically mutate ``<codex_root>/pool.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else pool_state_path()
        self._path = self._path.expanduser().resolve()
        self._root = self._path.parent
        self._lock_path = self._root / "state.lock"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def state_lock(self, timeout: float = 1.0) -> Iterator[None]:
        _private_dir(self._root)
        refresh_lock_path = self._root / "quota-v1.refresh.lock"
        if not refresh_lock_path.exists():
            try:
                fd = os.open(refresh_lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        if os.name != "nt":
            refresh_lock_path.chmod(0o600)
        lock = FileLock(str(self._lock_path), timeout=timeout)
        try:
            with lock:
                if os.name != "nt" and self._lock_path.exists():
                    self._lock_path.chmod(0o600)
                yield
        except Timeout as exc:
            raise AccountError("state lock unavailable") from exc

    def _load_unlocked(self) -> dict[str, Account]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccountError("corrupt account state") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("accounts", {}), dict):
            raise AccountError("corrupt account state")
        result: dict[str, Account] = {}
        for ref, entry in raw["accounts"].items():
            if not isinstance(ref, str) or not isinstance(entry, dict):
                raise AccountError("corrupt account state")
            try:
                auth_path = entry["auth_path"]
                weight = entry.get("weight", 1)
                imported_at = entry.get("imported_at", 0.0)
                enabled = entry.get("enabled", True)
                epoch = entry.get("quota_epoch", LEGACY_QUOTA_EPOCH)
                if not isinstance(auth_path, str) or not auth_path:
                    raise ValueError
                if isinstance(weight, bool) or int(weight) <= 0:
                    raise ValueError
                if not isinstance(enabled, bool):
                    raise ValueError
                if not isinstance(epoch, str) or not epoch:
                    raise ValueError
                result[ref] = Account(
                    ref=ref,
                    auth_path=auth_path,
                    enabled=enabled,
                    weight=int(weight),
                    imported_at=float(imported_at),
                    quota_epoch=epoch,
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                raise AccountError("corrupt account state") from None
        return result

    def _save_unlocked(self, accounts: dict[str, Account]) -> None:
        payload = {
            "accounts": {
                ref: {
                    "auth_path": account.auth_path,
                    "enabled": account.enabled,
                    "weight": account.weight,
                    "imported_at": account.imported_at,
                    "quota_epoch": account.quota_epoch,
                }
                for ref, account in sorted(accounts.items())
            }
        }
        _atomic_json(self._path, payload)

    def _invalidate_unlocked(self, refs: set[str]) -> None:
        if refs:
            from .quota_store import QuotaStore

            QuotaStore(self._root).invalidate_unlocked(refs)

    def list(self) -> list[Account]:
        with self.state_lock():
            accounts = self._load_unlocked()
        return sorted(accounts.values(), key=lambda account: account.ref)

    def get(self, ref: str) -> Account:
        with self.state_lock():
            account = self._load_unlocked().get(ref)
        if account is None:
            raise AccountError(f"unknown account ref: {ref}")
        return account

    def import_account(self, ref: str, auth_path: str, *, weight: int = 1) -> Account:
        if not isinstance(ref, str) or not ref or "/" in ref or "\\" in ref or ref in {".", ".."}:
            raise AccountError("account ref must be a non-empty path-safe string")
        if not isinstance(auth_path, str) or not auth_path:
            raise AccountError("auth path is required")
        # Resolve a new CLI reference at import time. This preserves the
        # user's working-directory meaning for ``--path ./auth.json`` while
        # keeping later proxy/TUI processes independent of their cwd.
        auth_path = os.path.abspath(os.path.expanduser(auth_path))
        if isinstance(weight, bool) or weight <= 0:
            raise AccountError("weight must be a positive integer")
        with self.state_lock():
            accounts = self._load_unlocked()
            previous = accounts.get(ref)
            account = Account(
                ref=ref,
                auth_path=auth_path,
                enabled=previous.enabled if previous else True,
                weight=previous.weight if previous else weight,
                imported_at=previous.imported_at if previous else time.time(),
                quota_epoch=str(uuid.uuid4()),
            )
            accounts[ref] = account
            self._save_unlocked(accounts)
            self._invalidate_unlocked({ref})
            return account

    def set_enabled(self, ref: str, enabled: bool) -> Account:
        with self.state_lock():
            accounts = self._load_unlocked()
            account = accounts.get(ref)
            if account is None:
                raise AccountError(f"unknown account ref: {ref}")
            account.enabled = bool(enabled)
            account.quota_epoch = str(uuid.uuid4())
            self._save_unlocked(accounts)
            self._invalidate_unlocked({ref})
            return account

    def set_auth_path(self, ref: str, auth_path: str) -> Account:
        if not isinstance(auth_path, str) or not auth_path:
            raise AccountError("auth path is required")
        auth_path = os.path.abspath(os.path.expanduser(auth_path))
        with self.state_lock():
            accounts = self._load_unlocked()
            account = accounts.get(ref)
            if account is None:
                raise AccountError(f"unknown account ref: {ref}")
            account.auth_path = auth_path
            account.quota_epoch = str(uuid.uuid4())
            self._save_unlocked(accounts)
            self._invalidate_unlocked({ref})
            return account

    def set_weight(self, ref: str, weight: int) -> Account:
        if isinstance(weight, bool) or weight <= 0:
            raise AccountError("weight must be a positive integer")
        with self.state_lock():
            accounts = self._load_unlocked()
            account = accounts.get(ref)
            if account is None:
                raise AccountError(f"unknown account ref: {ref}")
            account.weight = int(weight)
            self._save_unlocked(accounts)
            return account

    def eligible(self) -> list[Account]:
        from .quota_store import QuotaStore

        store = QuotaStore(self._root)
        return [account for account in self.list() if store.is_eligible(account)]


__all__ = ["Account", "AccountError", "AccountStore", "LEGACY_QUOTA_EPOCH"]
