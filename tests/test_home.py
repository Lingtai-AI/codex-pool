from __future__ import annotations

from subs_pool.modules.codex.home import data_home


def test_codex_uses_namespace_under_canonical_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("SUBS_POOL_HOME", str(root))
    monkeypatch.delenv("CODEX_POOL_HOME", raising=False)

    assert data_home() == root / "codex"


def test_codex_home_override_points_directly_to_module_directory(tmp_path, monkeypatch):
    root = tmp_path / "root"
    override = tmp_path / "existing-codex-data"
    override.mkdir()
    (override / "pool.json").write_text('{"accounts": []}', encoding="utf-8")
    monkeypatch.setenv("SUBS_POOL_HOME", str(root))
    monkeypatch.setenv("CODEX_POOL_HOME", str(override))

    assert data_home() == override
    assert (data_home() / "pool.json").is_file()
    assert not (root / "codex").exists()
