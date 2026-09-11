from __future__ import annotations

from subs_pool import cli
from subs_pool.registry import BUILTIN_MODULES, get_module


def test_registry_contains_only_real_codex_module():
    assert [(module.id, module.display_name) for module in BUILTIN_MODULES] == [
        ("codex", "Codex")
    ]
    assert get_module("codex") is BUILTIN_MODULES[0]


def test_modules_json_lists_explicit_builtin(capsys):
    assert cli.main(["modules", "list"]) == 0
    assert "codex" in capsys.readouterr().out


def test_codex_dispatch_preserves_module_json_payload(capsys):
    assert cli.main(["codex", "account", "list"]) == 0
    assert "(no accounts imported)" in capsys.readouterr().out


def test_unknown_module_has_json_error(capsys):
    assert cli.main(["unknown", "status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown subscription module" in captured.err


def test_bare_and_tui_dispatch_carry_selected_module(monkeypatch):
    selected: list[str] = []
    monkeypatch.setattr("subs_pool.tui.run_tui", selected.append)

    assert cli.main([]) == 0
    assert cli.main(["tui", "codex"]) == 0
    assert selected == ["codex", "codex"]
