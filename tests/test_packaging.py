"""Packaging metadata tests: keep Python and npm/bun install paths in sync."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _package_json() -> dict:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def test_python_metadata_is_installable() -> None:
    project = _pyproject()["project"]
    assert project["name"] == "szswap"
    assert project["requires-python"] == ">=3.12"
    assert project["readme"] == "README.md"
    assert project["license-files"] == ["LICENSE"]
    assert project["scripts"]["szswap"] == "szswap.cli:main"
    assert (ROOT / "README.md").is_file()
    assert (ROOT / "LICENSE").is_file()


def test_versions_match_between_python_and_npm() -> None:
    pyproject = _pyproject()
    package = _package_json()
    assert package["version"] == pyproject["project"]["version"]
    assert package["name"] == pyproject["project"]["name"]


def test_npm_wrapper_files_exist() -> None:
    package = _package_json()
    for bin_path in package["bin"].values():
        assert (ROOT / bin_path).is_file(), bin_path
    for entry in package["files"]:
        matches = list(ROOT.glob(entry))
        assert matches, f"npm files entry matches nothing: {entry}"
    # No install-time lifecycle script: npm runs postinstall with a cwd that does
    # not exist yet for `npm i -g git+https://...`, which aborts the install.
    # The bin shim bootstraps lazily instead.
    assert "postinstall" not in package.get("scripts", {})
    assert "prepare" not in package.get("scripts", {})
    assert package["scripts"]["szswap-setup"] == "node npm/install.js"
    assert (ROOT / "npm" / "install.js").is_file()


def test_npm_tarball_never_ships_pycache() -> None:
    """`files` uses an explicit ``src/szswap/*.py`` glob for this reason."""
    files = _package_json()["files"]
    assert "src/szswap/*.py" in files
    assert "src/szswap" not in files
