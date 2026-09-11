"""Thin TUI dispatcher for the distribution's static built-in modules."""

from __future__ import annotations

from typing import Any

from .registry import DEFAULT_MODULE_ID, get_module


def run_tui(module_id: str = DEFAULT_MODULE_ID, client: Any | None = None) -> None:
    """Dispatch to the selected built-in module's owned TUI."""
    module = get_module(module_id)
    if module.id == "codex":
        from .modules.codex.tui import run_tui as run_codex_tui

        run_codex_tui(module.id, client=client)
        return
    raise ValueError(f"module {module.id!r} does not provide a TUI")


__all__ = ["run_tui"]
