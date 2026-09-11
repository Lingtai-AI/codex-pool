from __future__ import annotations

import tomllib
from pathlib import Path


def test_distribution_name_entrypoint_and_wheel_package():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["name"] == "subs-pool"
    assert config["project"]["dynamic"] == ["version"]
    assert config["project"]["scripts"] == {
        "subspool": "subs_pool.cli:main",
        "subspool-cli": "subs_pool.agent_cli:main",
    }
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/subs_pool"
    ]
