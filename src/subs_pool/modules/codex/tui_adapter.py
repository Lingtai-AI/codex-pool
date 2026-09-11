"""Codex-owned TUI adapter.

Local account/status/quota actions execute the same handlers in-process. Only
the long-running human device-login stream retains the cancellable CLI bridge.
"""

from __future__ import annotations

import asyncio
import threading

from ...cli_client import CLIClient, CLIError, LoginStream
from .accounts import AccountError, AccountStore
from .cli import OperationError, account_list_data, quota_data, status_data
from .quota_refresh import QuotaRefreshCoordinator


class CodexTUIAdapter:
    def __init__(self) -> None:
        self.store = AccountStore()
        self.coordinator = QuotaRefreshCoordinator(self.store)
        self._login_client = CLIClient("codex")

    async def accounts_list(self) -> dict:
        return await asyncio.to_thread(account_list_data, self.store)

    async def accounts_import(self, ref: str, path: str, *, weight: int = 1) -> dict:
        def operation() -> dict:
            try:
                account = self.store.import_account(ref, path, weight=weight)
            except AccountError as exc:
                raise CLIError(str(exc)) from exc
            return account.to_status_dict(root=self.store.root)
        return await asyncio.to_thread(operation)

    async def pool_enable(self, ref: str) -> dict:
        return await self._toggle(ref, True)

    async def pool_disable(self, ref: str) -> dict:
        return await self._toggle(ref, False)

    async def _toggle(self, ref: str, enabled: bool) -> dict:
        def operation() -> dict:
            try:
                account = self.store.set_enabled(ref, enabled)
            except AccountError as exc:
                raise CLIError(str(exc)) from exc
            return account.to_status_dict(root=self.store.root)
        return await asyncio.to_thread(operation)

    async def pool_weight(self, ref: str, weight: int) -> dict:
        def operation() -> dict:
            try:
                account = self.store.set_weight(ref, weight)
            except AccountError as exc:
                raise CLIError(str(exc)) from exc
            return account.to_status_dict(root=self.store.root)
        return await asyncio.to_thread(operation)

    async def status(self) -> dict:
        def operation() -> dict:
            try:
                return status_data(self.store, self.coordinator.store)
            except OperationError as exc:
                raise CLIError(exc.message) from exc
        return await asyncio.to_thread(operation)

    async def quota(self) -> dict:
        cancel_event = threading.Event()

        def operation() -> dict:
            try:
                data, rc = quota_data(self.store, coordinator=self.coordinator, cancel_event=cancel_event)
            except OperationError as exc:
                raise CLIError(exc.message) from exc
            if rc:
                refresh = data.get("refresh", {})
                error = refresh.get("error") if isinstance(refresh, dict) else None
                message = error.get("message", "quota unavailable") if isinstance(error, dict) else "quota unavailable"
                raise CLIError(str(message), data=data)
            return data
        work = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancel_event.set()
            # Do not leave a sync quota owner hidden in the default executor.
            # Its lock and state cleanup are complete only after the
            # cooperative worker has actually stopped.
            await asyncio.shield(work)
            raise

    def login(self, ref: str) -> LoginStream:
        return self._login_client.login(ref)


__all__ = ["CodexTUIAdapter"]
