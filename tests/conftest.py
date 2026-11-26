"""Shared test isolation: never touch the real ~/.szswap or ~/.claude."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_szswap_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SZSWAP_HOME", str(tmp_path / "szswap-home"))
    monkeypatch.delenv("SZSWAP_CLAUDE_DIR", raising=False)
