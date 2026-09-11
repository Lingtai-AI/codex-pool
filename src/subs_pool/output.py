"""Small machine-envelope helpers used only by the Agent executable."""

from __future__ import annotations

from typing import Any


def envelope(command: str, *, ok: bool, data: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": command,
        "ok": ok,
        "data": data,
        "error": error,
    }


def error_object(code: str, message: str, *, details: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message[:256], "details": details if details is not None else {}}


__all__ = ["envelope", "error_object"]
