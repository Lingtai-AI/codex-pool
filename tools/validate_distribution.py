"""Small standard-library gate for the two release artifacts."""

from __future__ import annotations

import ast
import re
import sys
import tarfile
import zipfile
from pathlib import Path

NAME = "subs-pool"
SCRIPTS = {"subspool", "subspool-cli"}
_STATE = re.compile(r"(^|/)(pool\.json|quota-v1\.json|state\.lock|quota-v1\.refresh\.lock|.*\.lock|.*\.tmp(?:\.|$)|auth(?:/|$)|cache(?:/|$)|tmp(?:/|$))")


def source_version(root: Path) -> str:
    tree = ast.parse((root / "src/subs_pool/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("source __version__ is missing")


def _bad_member(member: str) -> str | None:
    if "codex_pool" in member or "__pycache__/" in member or member.endswith(".pyc"):
        return "old package or bytecode"
    if _STATE.search(member):
        return "runtime state"
    return None


def _metadata(raw: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            result.setdefault(key, value)
    return result


def validate_wheel(path: Path, version: str) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            if (bad := _bad_member(name)):
                errors.append(f"{path.name}: {name}: {bad}")
        metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            errors.append(f"{path.name}: expected one METADATA")
        else:
            metadata = _metadata(archive.read(metadata_names[0]))
            if metadata.get("Name") != NAME or metadata.get("Version") != version:
                errors.append(f"{path.name}: wrong name/version metadata")
        entry_names = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        if len(entry_names) != 1:
            errors.append(f"{path.name}: expected entry_points.txt")
        else:
            entries = []
            in_console = False
            for line in archive.read(entry_names[0]).decode("utf-8").splitlines():
                if line.strip() == "[console_scripts]":
                    in_console = True
                elif line.startswith("["):
                    in_console = False
                elif in_console and "=" in line:
                    entries.append(line.split("=", 1)[0].strip())
            if set(entries) != SCRIPTS or len(entries) != 2:
                errors.append(f"{path.name}: console scripts must be exactly {sorted(SCRIPTS)}")
        if not any(n.startswith("subs_pool/") for n in names):
            errors.append(f"{path.name}: subs_pool package is missing")
    return errors


def validate_sdist(path: Path, version: str) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        for name in names:
            if (bad := _bad_member(name)):
                errors.append(f"{path.name}: {name}: {bad}")
        pyprojects = [n for n in names if n.endswith("/pyproject.toml")]
        if len(pyprojects) != 1:
            errors.append(f"{path.name}: expected one pyproject.toml")
        else:
            import tomllib
            data = tomllib.loads(archive.extractfile(pyprojects[0]).read().decode("utf-8"))  # type: ignore[union-attr]
            project = data.get("project", {})
            if project.get("name") != NAME or "version" not in project.get("dynamic", []):
                errors.append(f"{path.name}: wrong project metadata")
    return errors


def validate(paths: list[Path], root: Path = Path(".")) -> list[str]:
    version = source_version(root)
    errors: list[str] = []
    for path in paths:
        if path.suffix == ".whl":
            errors.extend(validate_wheel(path, version))
        elif path.name.endswith(".tar.gz"):
            errors.extend(validate_sdist(path, version))
        else:
            errors.append(f"unsupported artifact: {path}")
    return errors


def main(argv: list[str] | None = None) -> int:
    paths = [Path(value) for value in (argv or sys.argv[1:])]
    errors = validate(paths)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
