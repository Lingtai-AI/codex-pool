"""Canonical data-root resolution owned by the subscription-pool core."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = "~/.subs-pool"


class HomeError(ValueError):
    """The selected subscription-pool root is not usable."""


def _resolve_env_path(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    if value == "":
        raise HomeError(f"{name} must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def data_home(*, create: bool = True) -> Path:
    """Return the canonical generic root.

    The core never searches for old roots.  Creation is explicit so machine
    help/version paths can remain state-free while stateful operations create
    only their selected root.
    """
    home = _resolve_env_path("SUBS_POOL_HOME", DEFAULT_HOME)
    if create:
        existed = home.exists()
        home.mkdir(parents=True, exist_ok=True)
        if not existed and os.name != "nt":
            home.chmod(0o700)
    return home


__all__ = ["DEFAULT_HOME", "HomeError", "data_home"]
