"""Machine-only ``subspool-cli`` entry point."""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .output import envelope, error_object
from .registry import BUILTIN_MODULES


def _emit(command: str, rc: int, data: Any = None, *, error: dict[str, Any] | None = None) -> int:
    if rc != 0 and error is None:
        refresh_error = data.get("refresh", {}).get("error") if isinstance(data, dict) else None
        if isinstance(refresh_error, dict):
            error = error_object(
                str(refresh_error.get("code", "operation_unavailable")),
                str(refresh_error.get("message", "operation did not complete")),
            )
        else:
            error = error_object("operation_unavailable", "operation did not complete")
    print(json.dumps(envelope(command, ok=rc == 0, data=data, error=error), ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return rc


def main(argv: list[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        return _emit("help", 0, {"usage": "subspool-cli [--help|--version] codex COMMAND"})
    if args == ["--help"]:
        return _emit("help", 0, {"usage": "subspool-cli codex account|status|quota"})
    if args == ["--version"]:
        return _emit("version", 0, {"version": __version__})
    if args[0] == "modules":
        if args[1:] == ["--help"]:
            return _emit("modules", 0, {"usage": "subspool-cli modules [list]"})
        if args[1:] not in ([], ["list"]):
            return _emit("modules", 2, None, error=error_object("invalid_syntax", "usage: subspool-cli modules [list]"))
        return _emit("modules", 0, {"modules": [{"id": m.id, "display_name": m.display_name} for m in BUILTIN_MODULES]})
    if args[0] == "tui":
        return _emit("tui", 2, None, error=error_object("unsupported_command", "the machine executable never opens the TUI"))
    if args[0] != "codex":
        return _emit("help", 2, None, error=error_object("unknown_module", "unknown subscription module"))
    command = "codex"
    if len(args) > 1:
        if args[1] != "--help":
            command = "codex." + args[1]
        if args[1] == "account" and len(args) > 2 and args[2] != "--help":
            command += "." + args[2]
    try:
        from .modules.codex.cli import machine_operation

        rc, data = machine_operation(args)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return _emit(command, rc, None, error=data["error"])
        return _emit(command, rc, data)
    except KeyboardInterrupt:
        return _emit(command, 130, None, error=error_object("interrupted", "operation interrupted"))
    except Exception:
        return _emit(command, 5, None, error=error_object("internal_error", "unexpected internal failure"))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
