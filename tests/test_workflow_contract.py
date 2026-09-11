from __future__ import annotations

from pathlib import Path


PINNED = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "astral-sh/setup-uv": "d0cc045d04ccac9d8b7881df0226f9e82c39688e",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
}


def test_publish_tuple_has_id_token_and_no_environment():
    path = Path(".github/workflows/publish.yml")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("name: Publish to PyPI\n")
    publish_job = text.split("\n  publish:\n", maxsplit=1)[1]
    assert "\n    environment:" not in publish_job
    assert "\n      id-token: write\n" in publish_job
    assert path.name == "publish.yml"


def test_build_workflow_has_tests_build_and_artifact_without_publish_step():
    text = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
    assert "pytest" in text
    assert "uv build" in text
    assert "actions/upload-artifact" in text
    assert "gh-action-pypi-publish" not in text
    assert "subspool-cli" in text


def test_all_third_party_actions_are_immutably_pinned():
    for workflow in (Path(".github/workflows/build.yml"), Path(".github/workflows/publish.yml")):
        text = workflow.read_text(encoding="utf-8")
        for action, sha in PINNED.items():
            if action in text:
                assert f"{action}@{sha}" in text
        assert "@v" not in text
        assert "@release/" not in text
