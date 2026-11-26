"""Unit tests for szSwap core modules: catalog, config, fetcher, and engine."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

import szswap
from szswap.catalog import DEFAULT_CLAUDE_MODELS, get_claude_model_rows
from szswap.config import (
    AppConfig,
    ConfigError,
    ProviderConfig,
    check_env_status,
    load_config,
    save_config,
    validate_base_url,
    validate_env_var_name,
    validate_provider_id,
)
from szswap.engine import (
    MANAGED_ENV_KEYS,
    EngineError,
    apply_hotswap,
    build_launch_spec,
    get_status,
    prepare_isolated_profile,
    read_claude_account_readonly,
    restore_anthropic,
)
from szswap.fetcher import (
    FetchError,
    build_models_urls,
    fetch_models_async,
    parse_models_payload,
)


@pytest.fixture(autouse=True)
def _disable_winreg_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep env lookups deterministic even on Windows hosts with user registry vars."""
    monkeypatch.setattr("szswap.config._resolve_windows_registry_env", lambda _name: None)


# ---------------------------------------------------------------------------
# Package & Catalog
# ---------------------------------------------------------------------------


def test_package_version() -> None:
    assert szswap.__version__ == "0.1.0"


def test_catalog_rows_include_active_and_custom_overrides() -> None:
    default_ids = [slot.id for slot in DEFAULT_CLAUDE_MODELS]
    assert "claude-opus-5-5" in default_ids
    assert "claude-haiku-4-5-20251001" in default_ids

    # Known alias ("opus") should not produce a duplicate row.
    rows_alias = get_claude_model_rows({}, claude_settings_model="opus")
    assert len(rows_alias) == len(DEFAULT_CLAUDE_MODELS)

    rows = get_claude_model_rows(
        {"claude-opus-5-5": "provider-a", "claude-custom-1": "provider-b"},
        claude_settings_model="claude-experimental-9",
    )
    row_ids = [r.id for r in rows]
    assert row_ids.count("claude-opus-5-5") == 1
    assert "claude-experimental-9" in row_ids
    assert "claude-custom-1" in row_ids


# ---------------------------------------------------------------------------
# Config validation & TOML round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good_id", ["mygw", "openrouter_1", "a", "my-proxy-01"])
def test_validate_provider_id_accepts_valid(good_id: str) -> None:
    assert validate_provider_id(good_id) == good_id


@pytest.mark.parametrize(
    "bad_id", ["", "anthropic", "default", "UPPER", "-dash", "bad space", "a" * 49]
)
def test_validate_provider_id_rejects_invalid(bad_id: str) -> None:
    with pytest.raises(ConfigError):
        validate_provider_id(bad_id)


@pytest.mark.parametrize("good_env", ["MY_GATEWAY_API_KEY", "_PRIVATE_KEY", "API_KEY_2"])
def test_validate_env_var_name_accepts_valid(good_env: str) -> None:
    assert validate_env_var_name(good_env) == good_env


@pytest.mark.parametrize(
    "bad_env",
    [
        "",
        "sk-ant-api03-secretvalue",
        "SK-1234567890",
        "MY KEY",
        "MY-KEY",
        "123_KEY",
        "MY_KEY\n",
    ],
)
def test_validate_env_var_name_rejects_secrets_and_invalid(bad_env: str) -> None:
    with pytest.raises(ConfigError):
        validate_env_var_name(bad_env)


def test_validate_base_url() -> None:
    assert validate_base_url("https://api.example.com///") == "https://api.example.com"
    assert validate_base_url("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080/v1"
    for bad in ("", "ftp://example.com", "https://", "not-a-url", "https://bad host/v1"):
        with pytest.raises(ConfigError):
            validate_base_url(bad)


def test_config_toml_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    provider = ProviderConfig(
        id="mygw",
        display_name="My Gateway",
        base_url="https://api.example.com/",
        auth_mode="bearer",
        auth_env="MY_GATEWAY_API_KEY",
        models_endpoint="/v1/models",
        default_model="claude-opus-5-5",
        model_overrides={"claude-opus-5-5": "glm-5.3"},
        cached_models=["glm-5.3", "deepseek-v4-pro", "glm-5.3"],
    )
    cfg = AppConfig(
        schema_version=1,
        default_provider="mygw",
        share_projects_history=False,
        providers={"mygw": provider},
    )

    saved_path = save_config(cfg, path=cfg_path)
    assert saved_path == cfg_path
    assert cfg_path.exists()

    loaded = load_config(path=cfg_path)
    assert loaded.schema_version == 1
    assert loaded.default_provider == "mygw"
    assert loaded.share_projects_history is False
    assert "mygw" in loaded.providers
    lp = loaded.providers["mygw"]
    assert lp.base_url == "https://api.example.com"
    assert lp.auth_env == "MY_GATEWAY_API_KEY"
    assert lp.default_model == "claude-opus-5-5"
    assert lp.model_overrides == {"claude-opus-5-5": "glm-5.3"}
    assert lp.cached_models == ["glm-5.3", "deepseek-v4-pro"]


def test_load_config_missing_without_env_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.delenv("MY_GATEWAY_API_KEY", raising=False)

    cfg = load_config(path=cfg_path)
    assert not cfg_path.exists()
    assert cfg.providers == {}
    assert check_env_status("MY_GATEWAY_API_KEY") == (False, 0)


# ---------------------------------------------------------------------------
# Fetcher URL normalization, payload parsing, and async fetch
# ---------------------------------------------------------------------------


def test_build_models_urls_avoids_double_v1_and_adds_fallback() -> None:
    assert build_models_urls("https://api.example.com", "/v1/models") == [
        "https://api.example.com/v1/models",
        "https://api.example.com/models",
    ]
    assert build_models_urls("https://api.example.com/v1", "/v1/models") == [
        "https://api.example.com/v1/models",
    ]
    assert build_models_urls("https://api.example.com/v1/", "/v1/custom") == [
        "https://api.example.com/v1/custom",
    ]


def test_parse_models_payload_formats_and_dedup() -> None:
    assert parse_models_payload(
        {"data": [{"id": "m1"}, {"id": "m2"}, {"id": "m1"}, {"name": "m3"}]}
    ) == ["m1", "m2", "m3"]
    assert parse_models_payload({"models": ["a", "b", "a"]}) == ["a", "b"]
    assert parse_models_payload([{"name": "x"}, "y", {"id": "x"}]) == ["x", "y"]
    with pytest.raises(FetchError):
        parse_models_payload({"unexpected": 123})


@pytest.mark.asyncio
async def test_fetch_models_async_bearer_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "super-secret-token-xyz")
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={"data": [{"id": "model-a"}, {"id": "model-b"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_models_async(
            base_url="https://proxy.example.com",
            auth_mode="bearer",
            auth_env="TEST_KEY",
            client=client,
        )

    assert result.status_code == 200
    assert result.endpoint_used == "https://proxy.example.com/v1/models"
    assert result.models == ["model-a", "model-b"]
    assert seen_headers.get("authorization") == "Bearer super-secret-token-xyz"


@pytest.mark.asyncio
async def test_fetch_models_async_x_api_key_and_404_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_KEY", "xapi-secret-999")
    requested_urls: list[str] = []
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        seen_headers.update(dict(request.headers))
        if request.url.path == "/v1/models":
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json={"models": ["fallback-model"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_models_async(
            base_url="https://proxy.example.com",
            auth_mode="x-api-key",
            auth_env="TEST_KEY",
            client=client,
        )

    assert requested_urls == [
        "https://proxy.example.com/v1/models",
        "https://proxy.example.com/models",
    ]
    assert result.endpoint_used == "https://proxy.example.com/models"
    assert result.models == ["fallback-model"]
    assert seen_headers.get("x-api-key") == "xapi-secret-999"
    assert seen_headers.get("anthropic-version") == "2023-06-01"


@pytest.mark.asyncio
async def test_fetch_models_async_missing_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    with pytest.raises(FetchError, match="MISSING_KEY"):
        await fetch_models_async(
            base_url="https://proxy.example.com",
            auth_mode="bearer",
            auth_env="MISSING_KEY",
        )


@pytest.mark.asyncio
async def test_fetch_models_async_redacts_secret_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-very-secret-leak-test-42"
    monkeypatch.setenv("TEST_KEY", secret)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"Invalid token Bearer {secret}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError) as exc_info:
            await fetch_models_async(
                base_url="https://proxy.example.com",
                auth_mode="bearer",
                auth_env="TEST_KEY",
                client=client,
            )

    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Engine: Mode A (isolated profile) & Mode B (hotswap / restore_anthropic)
# ---------------------------------------------------------------------------


def _make_fake_claude_dir(base: Path) -> Path:
    cdir = base / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    for sub in ("skills", "hooks", "plugins", "commands", "agents", "projects"):
        (cdir / sub).mkdir()
        (cdir / sub / "sample.txt").write_text(f"from {sub}", encoding="utf-8")

    (cdir / "CLAUDE.md").write_text("# User instructions\n", encoding="utf-8")
    (cdir / "keybindings.json").write_text('{"bindings": []}\n', encoding="utf-8")

    # Sensitive OAuth/local files that MUST NEVER be copied or linked into Mode A.
    (cdir / ".credentials.json").write_text('{"oauth": "secret"}\n', encoding="utf-8")
    (cdir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "user@example.com"}}),
        encoding="utf-8",
    )
    (cdir / "settings.local.json").write_text('{"local": true}\n', encoding="utf-8")

    (cdir / "settings.json").write_text(
        json.dumps(
            {
                "model": "claude-opus-5-5",
                "permissions": {"allow": ["Bash(git status)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash"}]},
                "effortLevel": "high",
                "env": {"CUSTOM_KEEP": "yes"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return cdir


def test_mode_a_profile_isolation_and_launch_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = _make_fake_claude_dir(tmp_path)
    sdir = tmp_path / ".szswap"
    secret = "mygw-live-token-do-not-write"
    monkeypatch.setenv("MY_GATEWAY_API_KEY", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://stale.example.com")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)

    provider = ProviderConfig(
        id="mygw",
        display_name="My Gateway",
        base_url="https://api.example.com",
        auth_mode="bearer",
        auth_env="MY_GATEWAY_API_KEY",
        default_model="claude-opus-5-5",
        model_overrides={"claude-opus-5-5": "glm-5.3"},
    )
    cfg = AppConfig(share_projects_history=True, providers={"mygw": provider})

    profile_dir = prepare_isolated_profile(provider, cfg, claude_dir=cdir, szswap_dir=sdir)
    assert profile_dir == sdir / "profiles" / "mygw"

    # Zero OAuth / auth files copied or linked.
    for forbidden in (".credentials.json", ".claude.json", "settings.local.json"):
        assert not (profile_dir / forbidden).exists()

    # Safe files copied and safe dirs linked.
    assert (profile_dir / "CLAUDE.md").read_text(encoding="utf-8") == "# User instructions\n"
    assert (profile_dir / "keybindings.json").exists()
    assert (profile_dir / "skills" / "sample.txt").read_text(encoding="utf-8") == "from skills"
    assert (profile_dir / "projects" / "sample.txt").read_text(encoding="utf-8") == "from projects"

    # Isolated settings.json has overrides, nonessential traffic disabled, and NO raw secret.
    settings_text = (profile_dir / "settings.json").read_text(encoding="utf-8")
    assert secret not in settings_text
    isolated = json.loads(settings_text)
    assert isolated["permissions"] == {"allow": ["Bash(git status)"]}
    assert isolated["hooks"] == {"PreToolUse": [{"matcher": "Bash"}]}
    assert isolated["modelOverrides"] == {"claude-opus-5-5": "glm-5.3"}
    assert isolated["env"] == {
        "ANTHROPIC_BASE_URL": "https://api.example.com",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }

    # build_launch_spec injects env safely and clears conflicting ANTHROPIC_* vars.
    spec = build_launch_spec(
        provider=provider,
        cfg=cfg,
        cwd=tmp_path,
        claude_args=["-p", "hello"],
        claude_dir=cdir,
        szswap_dir=sdir,
    )
    assert spec.env["CLAUDE_CONFIG_DIR"] == str(profile_dir)
    assert spec.env["ANTHROPIC_BASE_URL"] == "https://api.example.com"
    assert spec.env["ANTHROPIC_AUTH_TOKEN"] == secret
    assert "ANTHROPIC_API_KEY" not in spec.env
    assert spec.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert spec.command == ["/usr/bin/claude", "--model", "claude-opus-5-5", "-p", "hello"]

    # Explicit --model in claude_args must not be duplicated.
    spec_custom = build_launch_spec(
        provider=provider,
        cfg=cfg,
        cwd=tmp_path,
        claude_args=["--model", "claude-sonnet-5-5"],
        claude_dir=cdir,
        szswap_dir=sdir,
    )
    assert spec_custom.command == ["/usr/bin/claude", "--model", "claude-sonnet-5-5"]


def test_mode_a_fails_closed_when_token_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = _make_fake_claude_dir(tmp_path)
    sdir = tmp_path / ".szswap"
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)

    provider = ProviderConfig(
        id="other",
        display_name="Other",
        base_url="https://other.example.com",
        auth_mode="x-api-key",
        auth_env="MISSING_PROVIDER_KEY",
    )
    cfg = AppConfig(providers={"other": provider})
    with pytest.raises(EngineError, match="MISSING_PROVIDER_KEY"):
        prepare_isolated_profile(provider, cfg, claude_dir=cdir, szswap_dir=sdir)


def test_mode_b_hotswap_and_restore_anthropic_preserve_unrelated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdir = _make_fake_claude_dir(tmp_path)
    sdir = tmp_path / ".szswap"
    monkeypatch.setenv("MY_GATEWAY_API_KEY", "hotswap-token-123")

    provider = ProviderConfig(
        id="mygw",
        display_name="My Gateway",
        base_url="https://api.example.com",
        auth_mode="bearer",
        auth_env="MY_GATEWAY_API_KEY",
        model_overrides={"claude-opus-5-5": "glm-5.3"},
    )
    cfg = AppConfig(providers={"mygw": provider})

    assert read_claude_account_readonly(cdir) == "user@example.com"
    initial_status = get_status(cfg, claude_dir=cdir)
    assert initial_status.mode == "anthropic"
    assert initial_status.account_email == "user@example.com"
    assert initial_status.claude_default_model == "claude-opus-5-5"

    # Apply Mode B hotswap.
    apply_hotswap(provider, claude_dir=cdir, szswap_dir=sdir)
    swapped = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
    assert swapped["model"] == "claude-opus-5-5"
    assert swapped["permissions"] == {"allow": ["Bash(git status)"]}
    assert swapped["hooks"] == {"PreToolUse": [{"matcher": "Bash"}]}
    assert swapped["env"]["CUSTOM_KEEP"] == "yes"
    assert swapped["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.com"
    assert swapped["env"]["ANTHROPIC_AUTH_TOKEN"] == "hotswap-token-123"
    assert swapped["env"]["ANTHROPIC_API_KEY"] == ""
    assert swapped["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == ""
    assert swapped["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == ""
    assert swapped["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == ""
    assert swapped["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert swapped["modelOverrides"] == {"claude-opus-5-5": "glm-5.3"}

    proxy_status = get_status(cfg, claude_dir=cdir)
    assert proxy_status.mode == "proxy"
    assert proxy_status.matched_provider_id == "mygw"
    assert proxy_status.nonessential_disabled is True
    assert proxy_status.model_overrides == {"claude-opus-5-5": "glm-5.3"}

    # Restore default Anthropic settings: original values come back exactly.
    restore_anthropic(claude_dir=cdir, szswap_dir=sdir)
    restored = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
    assert restored["model"] == "claude-opus-5-5"
    assert restored["permissions"] == {"allow": ["Bash(git status)"]}
    assert restored["hooks"] == {"PreToolUse": [{"matcher": "Bash"}]}
    assert restored["env"]["CUSTOM_KEEP"] == "yes"
    assert "modelOverrides" not in restored
    for key in MANAGED_ENV_KEYS:
        assert key not in restored["env"]

    restored_status = get_status(cfg, claude_dir=cdir)
    assert restored_status.mode == "anthropic"
    assert restored_status.matched_provider_id is None
    assert restored_status.model_overrides == {}
    assert restored_status.nonessential_disabled is False
