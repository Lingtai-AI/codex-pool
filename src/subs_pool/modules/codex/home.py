"""Codex module data-root resolution.

``CODEX_POOL_HOME`` is an explicit direct override for an existing Codex pool
directory. Otherwise the module uses the ``codex`` namespace under the core's
``SUBS_POOL_HOME`` root. No legacy location is scanned or migrated.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...home import HomeError, data_home as subscription_pool_home


def data_home(*, create: bool = True) -> Path:
    """Return the direct Codex root selected by the documented precedence."""
    override = os.environ.get("CODEX_POOL_HOME")
    if override is not None:
        if override == "":
            raise HomeError("CODEX_POOL_HOME must not be empty")
        home = Path(override).expanduser()
        if not home.is_absolute():
            home = Path.cwd() / home
        home = home.resolve()
    else:
        home = subscription_pool_home(create=create) / "codex"
    if create:
        existed = home.exists()
        home.mkdir(parents=True, exist_ok=True)
        if not existed and os.name != "nt":
            home.chmod(0o700)
    return home


def pool_state_path() -> Path:
    return data_home() / "pool.json"
