"""Headless Textual TUI tests for szSwap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from szswap.config import AppConfig, ProviderConfig, get_config_path, load_config, save_config
from szswap.fetcher import FetchResult
from szswap.tui import SzSwapApp

SECRET_TOKEN = "dummy-token-12345"


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    szswap_home = tmp_path / "szswap"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SZSWAP_HOME", str(szswap_home))
    monkeypatch.setenv("SZSWAP_CLAUDE_DIR", str(claude_dir))
    monkeypatch.setenv("MY_GW_KEY", SECRET_TOKEN)
    monkeypatch.delenv("MY_GATEWAY_API_KEY", raising=False)
    return szswap_home, claude_dir


async def test_add_provider_fetch_map_and_save(isolated_env: tuple[Path, Path]) -> None:
    _, _ = isolated_env
    fake_models = ["glm-5.3", "deepseek-v4-pro", "kimi-k3"]

    async def fake_fetch(
        base_url: str,
        auth_mode: str,
        auth_env: str,
        models_endpoint: str = "/v1/models",
    ) -> FetchResult:
        return FetchResult(
            models=list(fake_models),
            endpoint_used=f"{base_url.rstrip('/')}/{models_endpoint.lstrip('/')}",
            status_code=200,
        )

    app = SzSwapApp(fetch_fn=fake_fetch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.click("#btn-add")
        await pilot.pause()

        screen = app.screen
        screen.query_one("#inp-id", Input).value = "mygw"
        screen.query_one("#inp-name", Input).value = "My Gateway"
        screen.query_one("#inp-url", Input).value = "https://gw.example.com"
        screen.query_one("#inp-auth-env", Input).value = "MY_GW_KEY"
        await pilot.pause()

        env_status = str(screen.query_one("#lbl-env-status", Static).render())
        assert f"len={len(SECRET_TOKEN)}" in env_status
        assert SECRET_TOKEN not in env_status

        await pilot.click("#btn-fetch-models")
        await app.workers.wait_for_complete()
        await pilot.pause()

        fetch_status = str(screen.query_one("#lbl-fetch-status", Static).render())
        assert "3 models" in fetch_status

        # First row in DEFAULT_CLAUDE_MODELS is claude-opus-5-5
        await pilot.click("#btn-pick-model")
        await pilot.pause()

        picker = app.screen
        picker.query_one("#inp-model-filter", Input).value = "glm"
        await pilot.pause()

        opt_list = picker.query_one("#opt-models", OptionList)
        assert opt_list.option_count == 1

        await pilot.click("#btn-picker-apply")
        await pilot.pause()

        await pilot.click("#btn-save")
        await pilot.pause()

    cfg = load_config()
    assert "mygw" in cfg.providers
    provider = cfg.providers["mygw"]
    assert provider.display_name == "My Gateway"
    assert provider.base_url == "https://gw.example.com"
    assert provider.auth_env == "MY_GW_KEY"
    assert provider.model_overrides["claude-opus-5-5"] == "glm-5.3"
    assert provider.cached_models == fake_models

    raw_toml = get_config_path().read_text(encoding="utf-8")
    assert SECRET_TOKEN not in raw_toml


async def test_hotswap_and_switch_to_anthropic(isolated_env: tuple[Path, Path]) -> None:
    _, claude_dir = isolated_env
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model": "claude-opus-5-5",
                "permissions": {"allow": ["Read"]},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cfg = AppConfig(
        default_provider="mygw",
        providers={
            "mygw": ProviderConfig(
                id="mygw",
                display_name="My Gateway",
                base_url="https://gw.example.com",
                auth_mode="bearer",
                auth_env="MY_GW_KEY",
                model_overrides={"claude-opus-5-5": "glm-5.3"},
            )
        },
    )
    save_config(cfg)

    app = SzSwapApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        initial_banner = str(app.query_one("#lbl-mode-status", Static).render())
        assert "Hot-Swap: OFF" in initial_banner
        assert "Anthropic Direct" in initial_banner

        await pilot.click("#btn-hotswap")
        await pilot.pause()
        await pilot.click("#btn-confirm-hotswap")
        await pilot.pause()

        swapped_banner = str(app.query_one("#lbl-mode-status", Static).render())
        assert "Hot-Swap: ON" in swapped_banner
        assert "mygw" in swapped_banner

        swapped_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert swapped_settings["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example.com"
        assert swapped_settings["env"]["ANTHROPIC_AUTH_TOKEN"] == SECRET_TOKEN
        assert swapped_settings["modelOverrides"]["claude-opus-5-5"] == "glm-5.3"
        assert swapped_settings["permissions"] == {"allow": ["Read"]}

        # Clicking Hot-Swap again toggles it OFF and clears managed env vars to "".
        await pilot.click("#btn-hotswap")
        await pilot.pause()

        restored_banner = str(app.query_one("#lbl-mode-status", Static).render())
        assert "Hot-Swap: OFF" in restored_banner
        assert "Anthropic Direct" in restored_banner

        restored_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        restored_env = restored_settings.get("env") or {}
        for key in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        ):
            assert key not in restored_env
        assert "modelOverrides" not in restored_settings
        assert restored_settings["permissions"] == {"allow": ["Read"]}


async def test_dashboard_arrow_navigation_and_quick_map(isolated_env: tuple[Path, Path]) -> None:
    _, _ = isolated_env
    cfg = AppConfig(
        default_provider="mygw",
        providers={
            "mygw": ProviderConfig(
                id="mygw",
                display_name="My Gateway",
                base_url="https://gw.example.com",
                auth_mode="bearer",
                auth_env="MY_GW_KEY",
                model_overrides={"claude-opus-5-5": "glm-5.3"},
                cached_models=["glm-5.3", "kimi-k3", "deepseek-v4-pro"],
            )
        },
    )
    save_config(cfg)

    app = SzSwapApp()
    async with app.run_test(size=(132, 36)) as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "provider-list"

        # Right jumps from provider list -> model overrides table; Left returns.
        await pilot.press("right")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "tbl-detail-overrides"

        await pilot.press("left")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "provider-list"

        # Down from last provider jumps into action bar; Left/Right cycle buttons; Up returns.
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-add"

        await pilot.press("right")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-edit"

        await pilot.press("left")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-add"

        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "provider-list"

        # Quick-map directly from dashboard overrides table using arrows + Enter in picker.
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        picker = app.screen
        filter_inp = picker.query_one("#inp-model-filter", Input)
        filter_inp.value = ""
        await pilot.pause()

        # Down arrow inside search input moves OptionList highlight directly.
        opt_list = picker.query_one("#opt-models", OptionList)
        assert opt_list.highlighted == 0
        await pilot.press("down")
        await pilot.pause()
        assert opt_list.highlighted == 1

        await pilot.press("enter")
        await pilot.pause()

    updated = load_config()
    assert updated.providers["mygw"].model_overrides["claude-opus-5-5"] == "kimi-k3"


async def test_editor_explains_env_var_name_vs_raw_key(
    isolated_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _ = isolated_env
    monkeypatch.delenv("SZ_API_KEY", raising=False)
    cfg = AppConfig(
        default_provider="sz",
        providers={
            "sz": ProviderConfig(
                id="sz",
                display_name="Seiz",
                base_url="https://sg.seiz.cloud/anthropic",
                auth_mode="bearer",
                auth_env="SZ_API_KEY",
            )
        },
    )
    save_config(cfg)

    app = SzSwapApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        screen = app.screen
        auth_input = screen.query_one("#inp-auth-env", Input)
        status = screen.query_one("#lbl-env-status", Static)

        # A pasted API key must be rejected with an explanation, not a regex dump.
        auth_input.value = "ah-0123456789abcdef0123456789abcdef"
        await pilot.pause()
        raw_key_status = str(status.render())
        assert "API key" in raw_key_status
        assert "SZ_API_KEY" in raw_key_status
        assert "^[" not in raw_key_status

        # A variable name is accepted; the UI says how to export it.
        auth_input.value = "SZ_API_KEY"
        await pilot.pause()
        name_status = str(status.render())
        assert "SZ_API_KEY" in name_status
        assert "not exported" in name_status


async def test_editor_and_modal_arrow_navigation(isolated_env: tuple[Path, Path]) -> None:
    _, _ = isolated_env
    cfg = AppConfig(
        default_provider="mygw",
        providers={
            "mygw": ProviderConfig(
                id="mygw",
                display_name="My Gateway",
                base_url="https://gw.example.com",
                auth_mode="bearer",
                auth_env="MY_GW_KEY",
            )
        },
    )
    save_config(cfg)

    app = SzSwapApp()
    async with app.run_test(size=(132, 36)) as pilot:
        await pilot.pause()
        # Press Enter on selected provider opens Editor; focus starts on inp-name (inp-id disabled).
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "inp-name"

        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "inp-url"

        await pilot.press("up")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "inp-name"

        await pilot.press("escape")
        await pilot.pause()

        # Confirm Delete modal supports Left/Right arrow switching between buttons.
        await pilot.click("#btn-delete")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-cancel-delete"

        await pilot.press("left")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-confirm-delete"

        await pilot.press("right")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-cancel-delete"

        await pilot.press("escape")
        await pilot.pause()
