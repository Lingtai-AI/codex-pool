"""Small, explicit registry for modules shipped in this distribution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

CommandMain = Callable[[list[str] | None], int]


@dataclass(frozen=True)
class ModuleDescriptor:
    """The complete interface the core needs from a built-in module."""

    id: str
    display_name: str
    command_main: CommandMain


class UnknownModuleError(ValueError):
    pass


def _codex_main(argv: list[str] | None = None) -> int:
    from .modules.codex.cli import main

    return main(argv)


BUILTIN_MODULES = (
    ModuleDescriptor(id="codex", display_name="Codex", command_main=_codex_main),
)
DEFAULT_MODULE_ID = "codex"
_BY_ID = {module.id: module for module in BUILTIN_MODULES}


def get_module(module_id: str) -> ModuleDescriptor:
    try:
        return _BY_ID[module_id]
    except KeyError as exc:
        available = ", ".join(_BY_ID)
        raise UnknownModuleError(
            f"unknown subscription module {module_id!r}; available: {available}"
        ) from exc


__all__ = [
    "BUILTIN_MODULES",
    "DEFAULT_MODULE_ID",
    "ModuleDescriptor",
    "UnknownModuleError",
    "get_module",
]
