from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "subs-pool-home"
    monkeypatch.setenv("SUBS_POOL_HOME", str(home))
    monkeypatch.delenv("CODEX_POOL_HOME", raising=False)
    return home
