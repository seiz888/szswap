"""Tests for the 0600 secrets store that lets the UI accept an API key directly."""

from __future__ import annotations

import getpass
import json
import os
import stat
from pathlib import Path

import pytest
from textual.widgets import Input

from szswap import cli
from szswap.config import (
    AppConfig,
    ConfigError,
    ProviderConfig,
    check_env_status,
    default_env_name,
    delete_secret,
    env_value_source,
    get_config_path,
    get_secrets_path,
    load_config,
    load_secrets,
    resolve_env_var,
    save_config,
    save_secret,
)
from szswap.engine import apply_hotswap, build_launch_spec, restore_anthropic
from szswap.tui import SzSwapApp

TEST_KEY = "ah-test-key-0123456789abcdef"


def _provider(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "id": "seiz",
        "display_name": "Seiz",
        "base_url": "https://sg.seiz.cloud/anthropic",
        "auth_mode": "bearer",
        "auth_env": "SZ_API_KEY",
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Secrets store
# ---------------------------------------------------------------------------


def test_save_resolve_and_delete_secret(tmp_path: Path) -> None:
    secrets_path = get_secrets_path()
    assert resolve_env_var("SZ_API_KEY") is None

    save_secret("SZ_API_KEY", TEST_KEY)
    assert secrets_path.exists()
    assert load_secrets() == {"SZ_API_KEY": TEST_KEY}
    assert resolve_env_var("SZ_API_KEY") == TEST_KEY
    assert env_value_source("SZ_API_KEY") == "store"
    assert check_env_status("SZ_API_KEY") == (True, len(TEST_KEY))

    delete_secret("SZ_API_KEY")
    assert not secrets_path.exists()
    assert resolve_env_var("SZ_API_KEY") is None
    assert not delete_secret("SZ_API_KEY")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_secrets_file_is_owner_only(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.json"
    save_secret("SZ_API_KEY", TEST_KEY, path=secrets_path)
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_environment_variable_wins_over_stored_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_secret("SZ_API_KEY", "from-store", path=tmp_path / "secrets.json")
    monkeypatch.setattr("szswap.config.get_secrets_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setenv("SZ_API_KEY", "from-env")

    assert resolve_env_var("SZ_API_KEY") == "from-env"
    assert env_value_source("SZ_API_KEY") == "env"


def test_save_secret_rejects_bad_values(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.json"
    with pytest.raises(ConfigError):
        save_secret("SZ_API_KEY", "   ", path=secrets_path)
    with pytest.raises(ConfigError):
        save_secret("SZ_API_KEY", "line1\nline2", path=secrets_path)
    with pytest.raises(ConfigError):
        save_secret("not a name", TEST_KEY, path=secrets_path)
    assert not secrets_path.exists()


def test_secret_never_reaches_config_toml(tmp_path: Path) -> None:
    cfg = AppConfig(providers={"seiz": _provider()})
    save_config(cfg, path=tmp_path / "config.toml")
    save_secret("SZ_API_KEY", TEST_KEY, path=tmp_path / "secrets.json")

    raw_toml = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert TEST_KEY not in raw_toml
    assert "SZ_API_KEY" in raw_toml


def test_default_env_name() -> None:
    assert default_env_name("seiz") == "SZ_SEIZ_API_KEY"
    assert default_env_name("my-gw_2") == "SZ_MY_GW_2_API_KEY"


# ---------------------------------------------------------------------------
# Engine integration: stored keys work for both modes
# ---------------------------------------------------------------------------


def test_mode_a_uses_stored_secret_without_writing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SZ_API_KEY", raising=False)
    monkeypatch.setattr("szswap.config.get_secrets_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr("szswap.engine.get_szswap_dir", lambda: tmp_path / "szswap")
    monkeypatch.setattr("szswap.engine.get_claude_dir", lambda: tmp_path / "claude")
    monkeypatch.setattr(
        "szswap.engine._resolve_claude_executable", lambda: "/usr/bin/claude"
    )
    (tmp_path / "claude").mkdir()
    save_secret("SZ_API_KEY", TEST_KEY, path=tmp_path / "secrets.json")

    provider = _provider()
    cfg = AppConfig(providers={"seiz": provider})
    spec = build_launch_spec(provider, cfg, cwd=tmp_path)

    assert spec.env["ANTHROPIC_AUTH_TOKEN"] == TEST_KEY
    profile_settings = (spec.profile_dir / "settings.json").read_text(encoding="utf-8")
    assert TEST_KEY not in profile_settings


def test_mode_b_uses_stored_secret_and_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SZ_API_KEY", raising=False)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-user-own"}}), encoding="utf-8"
    )
    monkeypatch.setattr("szswap.config.get_secrets_path", lambda: tmp_path / "secrets.json")
    save_secret("SZ_API_KEY", TEST_KEY, path=tmp_path / "secrets.json")

    apply_hotswap(_provider(), claude_dir=claude_dir, szswap_dir=tmp_path / "szswap")
    swapped = json.loads(settings_path.read_text(encoding="utf-8"))
    assert swapped["env"]["ANTHROPIC_AUTH_TOKEN"] == TEST_KEY

    restore_anthropic(claude_dir=claude_dir, szswap_dir=tmp_path / "szswap")
    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert restored["env"] == {"ANTHROPIC_API_KEY": "sk-user-own"}


# ---------------------------------------------------------------------------
# UI + CLI entry points
# ---------------------------------------------------------------------------


async def test_ui_accepts_pasted_key_and_stores_it(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    app = SzSwapApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-add")
        await pilot.pause()

        screen = app.screen
        screen.query_one("#inp-id", Input).value = "seiz"
        screen.query_one("#inp-name", Input).value = "Seiz"
        screen.query_one("#inp-url", Input).value = "https://sg.seiz.cloud/anthropic"
        screen.query_one("#inp-api-key", Input).value = TEST_KEY
        await pilot.pause()
        await pilot.click("#btn-save")
        await pilot.pause()

    saved = load_config().providers["seiz"]
    assert saved.auth_env == "SZ_SEIZ_API_KEY"
    assert load_secrets()[saved.auth_env] == TEST_KEY
    assert TEST_KEY not in get_config_path().read_text(encoding="utf-8")
    assert get_secrets_path().exists()

    # Re-opening the editor shows that a key is already stored.
    app = SzSwapApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        placeholder = app.screen.query_one("#inp-api-key", Input).placeholder
        assert "stored" in placeholder


async def test_deleting_provider_removes_its_stored_key(tmp_path: Path) -> None:
    save_secret("SZ_API_KEY", TEST_KEY)
    save_config(AppConfig(default_provider="seiz", providers={"seiz": _provider()}))

    app = SzSwapApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause()
        await pilot.click("#btn-delete")
        await pilot.pause()
        await pilot.click("#btn-confirm-delete")
        await pilot.pause()

    assert "seiz" not in load_config().providers
    assert load_secrets() == {}


async def test_renaming_env_var_migrates_stored_key(tmp_path: Path) -> None:
    save_secret("SZ_API_KEY", TEST_KEY)
    save_config(AppConfig(default_provider="seiz", providers={"seiz": _provider()}))

    app = SzSwapApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#inp-auth-env", Input).value = "SZ_RENAMED_KEY"
        await pilot.pause()
        await pilot.click("#btn-save")
        await pilot.pause()

    assert load_config().providers["seiz"].auth_env == "SZ_RENAMED_KEY"
    assert load_secrets() == {"SZ_RENAMED_KEY": TEST_KEY}


def test_cli_key_set_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(AppConfig(providers={"seiz": _provider(auth_env="")}))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": TEST_KEY)

    assert cli.main(["key", "set", "seiz"]) == 0
    configured = load_config().providers["seiz"]
    assert configured.auth_env == "SZ_SEIZ_API_KEY"
    assert load_secrets()[configured.auth_env] == TEST_KEY

    assert cli.main(["key", "clear", "seiz"]) == 0
    assert load_secrets() == {}
