"""Regression tests for the issues found during the szSwap code review."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from textual.widgets import Input

from szswap import cli
from szswap.catalog import get_claude_model_rows
from szswap.config import (
    AppConfig,
    ProviderConfig,
    load_config,
    save_config,
)
from szswap.engine import (
    EngineError,
    apply_hotswap,
    get_status,
    prepare_isolated_profile,
    restore_anthropic,
)
from szswap.tui import ConfirmReplaceProviderModal, SzSwapApp


@pytest.fixture(autouse=True)
def _disable_winreg_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("szswap.config._resolve_windows_registry_env", lambda _name: None)


def _write_provider_config(path: Path, provider: ProviderConfig) -> None:
    save_config(AppConfig(providers={provider.id: provider}), path=path)


def _make_provider(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "id": "mygw",
        "display_name": "My Gateway",
        "base_url": "https://gw.example.com",
        "auth_mode": "bearer",
        "auth_env": "MY_GW_KEY",
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CLI: --dir and claude passthrough
# ---------------------------------------------------------------------------


def test_cli_run_parses_dir_and_forwarded_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SZSWAP_HOME", str(tmp_path / "szswap"))
    monkeypatch.setenv("MY_GW_KEY", "token")
    _write_provider_config(tmp_path / "szswap" / "config.toml", _make_provider())

    captured: dict[str, object] = {}

    def fake_run(provider: ProviderConfig, cfg: AppConfig, cwd: Path | None = None, claude_args: list[str] | None = None) -> int:
        captured.update(provider=provider.id, cwd=cwd, claude_args=claude_args)
        return 0

    monkeypatch.setattr(cli, "run_isolated", fake_run)

    assert cli.main(["run", "mygw", "--dir", str(tmp_path), "--", "-p", "hello"]) == 0
    assert captured["provider"] == "mygw"
    assert captured["cwd"] == tmp_path
    assert captured["claude_args"] == ["-p", "hello"]

    # Unknown flags without an explicit "--" are still forwarded to claude.
    assert cli.main(["run", "mygw", "--model", "opus"]) == 0
    assert captured["claude_args"] == ["--model", "opus"]

    # Mixed flag/non-flag order is preserved and --dir still parses anywhere.
    assert cli.main(["run", "mygw", "-p", "hi", "--dir", str(tmp_path), "-v"]) == 0
    assert captured["cwd"] == tmp_path
    assert captured["claude_args"] == ["-p", "hi", "-v"]


def test_cli_run_reports_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SZSWAP_HOME", str(tmp_path / "szswap"))
    monkeypatch.setenv("MY_GW_KEY", "token")
    _write_provider_config(tmp_path / "szswap" / "config.toml", _make_provider())

    rc = cli.main(["run", "mygw", "--dir", str(tmp_path / "does-not-exist")])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_rejects_unknown_args_for_non_run_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status", "--bogus"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_python_dash_m_entrypoint(tmp_path: Path) -> None:
    env = {**os.environ, "SZSWAP_HOME": str(tmp_path / "szswap")}
    proc = subprocess.run(
        [sys.executable, "-m", "szswap", "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "szswap" in proc.stdout


def test_cli_version_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SZSWAP_HOME", str(tmp_path / "szswap"))
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    assert "szswap 0.1.0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Config: tolerate broken entries, round-trip extra Claude slots
# ---------------------------------------------------------------------------


def test_load_config_skips_invalid_provider_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "default_provider = 'broken'",
                "",
                "[providers.good]",
                "id = 'good'",
                "base_url = 'https://good.example.com'",
                "auth_env = 'GOOD_KEY'",
                "",
                "[providers.broken]",
                "id = 'broken'",
                "base_url = ''",
                "auth_env = 'BROKEN_KEY'",
                "",
                "[providers.mismatch]",
                "id = 'real-id'",
                "base_url = 'https://mismatch.example.com'",
                "auth_env = 'MISMATCH_KEY'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(path=cfg_path)
    assert set(cfg.providers) == {"good"}
    assert cfg.default_provider == ""
    stderr = capsys.readouterr().err
    assert "skipping invalid provider 'broken'" in stderr
    assert "skipping provider 'mismatch'" in stderr


def test_config_roundtrips_extra_claude_model_slots(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg = AppConfig(
        providers={"mygw": _make_provider()},
        claude_model_slots=["claude-opus-6", "claude-opus-6", "  ", "claude-sonnet-6"],
    )
    save_config(cfg, path=cfg_path)
    loaded = load_config(path=cfg_path)
    assert loaded.claude_model_slots == ["claude-opus-6", "claude-sonnet-6"]

    rows = get_claude_model_rows({}, extra_slots=loaded.claude_model_slots)
    row_ids = [row.id for row in rows]
    assert "claude-opus-6" in row_ids
    assert "claude-sonnet-6" in row_ids


# ---------------------------------------------------------------------------
# Engine: status detection, Hot-Swap backup/restore, safe linking
# ---------------------------------------------------------------------------


def test_get_status_ignores_user_owned_auth_env(tmp_path: Path) -> None:
    cdir = tmp_path / "claude"
    cdir.mkdir()
    (cdir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-user-owned-key"}}),
        encoding="utf-8",
    )

    status = get_status(AppConfig(), claude_dir=cdir)
    assert status.mode == "anthropic"
    assert status.base_url == ""


def test_hotswap_restore_preserves_user_previous_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = tmp_path / "claude"
    cdir.mkdir()
    sdir = tmp_path / "szswap"
    monkeypatch.setenv("MY_GW_KEY", "new-token")

    settings_path = cdir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model": "claude-opus-5-5",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://user-owned.example.com",
                    "ANTHROPIC_API_KEY": "sk-user-owned",
                    "CUSTOM_KEEP": "yes",
                },
                "modelOverrides": {"claude-opus-5-5": "user-model"},
            }
        ),
        encoding="utf-8",
    )

    provider = _make_provider(model_overrides={"claude-opus-5-5": "glm-5.3"})
    apply_hotswap(provider, claude_dir=cdir, szswap_dir=sdir)

    swapped = json.loads(settings_path.read_text(encoding="utf-8"))
    assert swapped["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example.com"
    assert swapped["env"]["ANTHROPIC_AUTH_TOKEN"] == "new-token"
    assert swapped["modelOverrides"] == {"claude-opus-5-5": "glm-5.3"}

    restore_anthropic(claude_dir=cdir, szswap_dir=sdir)
    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert restored["env"]["ANTHROPIC_BASE_URL"] == "https://user-owned.example.com"
    assert restored["env"]["ANTHROPIC_API_KEY"] == "sk-user-owned"
    assert "ANTHROPIC_AUTH_TOKEN" not in restored["env"]
    assert restored["env"]["CUSTOM_KEEP"] == "yes"
    assert restored["modelOverrides"] == {"claude-opus-5-5": "user-model"}
    assert not (sdir / "state.json").exists()


def test_switching_providers_keeps_original_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = tmp_path / "claude"
    cdir.mkdir()
    sdir = tmp_path / "szswap"
    monkeypatch.setenv("MY_GW_KEY", "token-a")
    monkeypatch.setenv("OTHER_KEY", "token-b")

    settings_path = cdir / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://user-owned.example.com"}}),
        encoding="utf-8",
    )

    provider_a = _make_provider()
    provider_b = _make_provider(
        id="other",
        base_url="https://other.example.com",
        auth_mode="x-api-key",
        auth_env="OTHER_KEY",
    )

    apply_hotswap(provider_a, claude_dir=cdir, szswap_dir=sdir)
    apply_hotswap(provider_b, claude_dir=cdir, szswap_dir=sdir)
    after_b = json.loads(settings_path.read_text(encoding="utf-8"))
    assert after_b["env"]["ANTHROPIC_BASE_URL"] == "https://other.example.com"
    assert after_b["env"]["ANTHROPIC_API_KEY"] == "token-b"

    restore_anthropic(claude_dir=cdir, szswap_dir=sdir)
    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert restored["env"]["ANTHROPIC_BASE_URL"] == "https://user-owned.example.com"
    assert "ANTHROPIC_API_KEY" not in restored["env"]


def test_restore_without_settings_file_does_not_create_it(tmp_path: Path) -> None:
    cdir = tmp_path / "claude"
    cdir.mkdir()
    restore_anthropic(claude_dir=cdir, szswap_dir=tmp_path / "szswap")
    assert not (cdir / "settings.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics only")
def test_hotswap_settings_file_is_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = tmp_path / "claude"
    cdir.mkdir()
    monkeypatch.setenv("MY_GW_KEY", "token")
    settings_path = cdir / "settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")
    settings_path.chmod(0o644)

    apply_hotswap(_make_provider(), claude_dir=cdir, szswap_dir=tmp_path / "szswap")
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


def test_profile_link_never_deletes_non_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = tmp_path / "claude"
    (cdir / "skills").mkdir(parents=True)
    (cdir / "skills" / "source.md").write_text("source", encoding="utf-8")

    sdir = tmp_path / "szswap"
    profile_skills = sdir / "profiles" / "mygw" / "skills"
    profile_skills.mkdir(parents=True)
    (profile_skills / "user-file.md").write_text("mine", encoding="utf-8")

    monkeypatch.setenv("MY_GW_KEY", "token")
    provider = _make_provider()
    with pytest.raises(EngineError, match="Refusing to delete non-empty directory"):
        prepare_isolated_profile(
            provider,
            AppConfig(providers={"mygw": provider}),
            claude_dir=cdir,
            szswap_dir=sdir,
        )
    assert (profile_skills / "user-file.md").read_text(encoding="utf-8") == "mine"


def test_profile_link_replaces_stale_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = tmp_path / "claude"
    (cdir / "skills").mkdir(parents=True)
    (cdir / "skills" / "source.md").write_text("source", encoding="utf-8")

    sdir = tmp_path / "szswap"
    profile_skills = sdir / "profiles" / "mygw" / "skills"
    stale = tmp_path / "stale"
    stale.mkdir()
    profile_skills.parent.mkdir(parents=True)
    os.symlink(stale, profile_skills, target_is_directory=True)

    monkeypatch.setenv("MY_GW_KEY", "token")
    provider = _make_provider()
    profile_dir = prepare_isolated_profile(
        provider,
        AppConfig(providers={"mygw": provider}),
        claude_dir=cdir,
        szswap_dir=sdir,
    )
    assert (profile_dir / "skills" / "source.md").read_text(encoding="utf-8") == "source"


# ---------------------------------------------------------------------------
# TUI: adding an existing provider id must be confirmed
# ---------------------------------------------------------------------------


async def test_add_existing_provider_id_requires_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    szswap_home = tmp_path / "szswap"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("SZSWAP_HOME", str(szswap_home))
    monkeypatch.setenv("SZSWAP_CLAUDE_DIR", str(claude_dir))
    monkeypatch.setenv("MY_GW_KEY", "token")

    save_config(
        AppConfig(
            default_provider="mygw",
            providers={
                "mygw": _make_provider(
                    display_name="Original",
                    base_url="https://old.example.com",
                )
            },
        )
    )

    async def _fill_and_save(pilot) -> None:  # type: ignore[no-untyped-def]
        await pilot.click("#btn-add")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#inp-id", Input).value = "mygw"
        screen.query_one("#inp-name", Input).value = "Replacement"
        screen.query_one("#inp-url", Input).value = "https://new.example.com"
        screen.query_one("#inp-auth-env", Input).value = "MY_GW_KEY"
        await pilot.pause()
        await pilot.click("#btn-save")
        await pilot.pause()

    app = SzSwapApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        # Cancel keeps the existing provider untouched.
        await _fill_and_save(pilot)
        assert isinstance(app.screen, ConfirmReplaceProviderModal)
        await pilot.click("#btn-cancel-replace")
        await pilot.pause()
        assert load_config().providers["mygw"].base_url == "https://old.example.com"

        # Confirming overwrites it.
        await _fill_and_save(pilot)
        assert isinstance(app.screen, ConfirmReplaceProviderModal)
        await pilot.click("#btn-confirm-replace")
        await pilot.pause()

    saved = load_config().providers["mygw"]
    assert saved.display_name == "Replacement"
    assert saved.base_url == "https://new.example.com"
