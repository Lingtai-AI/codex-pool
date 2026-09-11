"""Human ``subspool`` dispatcher."""

from __future__ import annotations

import sys

from . import __version__
from .registry import BUILTIN_MODULES, DEFAULT_MODULE_ID, UnknownModuleError, get_module

_USAGE = """usage: subspool [tui | modules [list] | codex COMMAND ...]

With no arguments, open the Codex account TUI.
  tui                 open the TUI
  modules [list]      list static built-in modules
  codex COMMAND       run a human Codex operation
  --version           print the version
"""


def _error(message: str, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _run_tui(module_id: str) -> int:
    try:
        module = get_module(module_id)
    except UnknownModuleError as exc:
        return _error(str(exc))
    from .tui import run_tui

    run_tui(module.id)
    return 0


def _list_modules() -> int:
    for module in BUILTIN_MODULES:
        marker = " (default)" if module.id == DEFAULT_MODULE_ID else ""
        print(f"{module.id:<16} {module.display_name}{marker}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        return _run_tui(DEFAULT_MODULE_ID)
    if args == ["--help"] or args == ["-h"]:
        print(_USAGE, end="")
        return 0
    if args == ["--version"]:
        print(f"subspool {__version__}")
        return 0
    if args[0] == "modules":
        rest = args[1:]
        if rest in ([], ["list"]):
            return _list_modules()
        return _error("usage: subspool modules [list]")
    if args[0] == "tui":
        if args[1:] in ([], ["codex"]):
            return _run_tui(DEFAULT_MODULE_ID)
        if args[1:] in (["--help"], ["-h"]):
            print("usage: subspool tui [codex]")
            return 0
        return _error("usage: subspool tui [codex]")
    try:
        module = get_module(args[0])
    except UnknownModuleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return module.command_main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
