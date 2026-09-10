"""Data-root resolution.

Every piece of persistent state (account/pool config) lives under one
data-root directory, overridable with ``CODEX_POOL_HOME`` so tests and
multiple installs stay isolated from each other and from any other tool's
config (notably ``~/.lingtai-tui`` and ``~/.codex``, which this package never
reads or writes).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = "~/.codex-pool"


def data_home() -> Path:
    """Return the codex-pool data-root, creating it if missing."""
    raw = os.environ.get("CODEX_POOL_HOME", DEFAULT_HOME)
    home = Path(raw).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home


def pool_state_path() -> Path:
    return data_home() / "pool.json"
