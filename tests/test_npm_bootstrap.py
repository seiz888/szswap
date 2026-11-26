"""Tests for the npm/bun bootstrap shim (skipped when node is unavailable)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

BOOTSTRAP = Path(__file__).resolve().parents[1] / "npm" / "lib" / "bootstrap.js"


def _make_layout(root: Path, init_source: str = "VALUE = 1\n") -> Path:
    (root / "npm" / "lib").mkdir(parents=True)
    shutil.copy2(BOOTSTRAP, root / "npm" / "lib" / "bootstrap.js")
    (root / "package.json").write_text(
        json.dumps({"name": "szswap", "version": "0.1.0"}), encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nname='szswap'\n", encoding="utf-8")
    src = root / "src" / "szswap"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(init_source, encoding="utf-8")
    return root


def _fingerprint(root: Path) -> str:
    script = f'process.stdout.write(require({json.dumps(str(root / "npm" / "lib" / "bootstrap.js"))}).sourceFingerprint())'
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def test_source_fingerprint_is_stable_and_sensitive(tmp_path: Path) -> None:
    root = _make_layout(tmp_path / "pkg")
    first = _fingerprint(root)
    assert len(first) == 64
    assert _fingerprint(root) == first

    # Changing a packaged source file must invalidate an existing venv marker,
    # otherwise `npm install` of a new build of the same version stays stale.
    (root / "src" / "szswap" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _fingerprint(root) != first


def test_venv_paths_follow_env_overrides(tmp_path: Path) -> None:
    root = _make_layout(tmp_path / "pkg")
    venv = tmp_path / "custom-venv"
    script = (
        f"const b = require({json.dumps(str(root / 'npm' / 'lib' / 'bootstrap.js'))});"
        "process.stdout.write(b.venvPython());"
    )
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SZSWAP_VENV": str(venv),
            "HOME": str(tmp_path),
        },
    )
    assert Path(result.stdout.strip()) == venv / "bin" / "python" or result.stdout.strip().endswith(
        "python.exe"
    )
