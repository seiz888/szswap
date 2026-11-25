"""Configuration schema, validation, env resolution, and atomic TOML I/O."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomli_w


class ConfigError(ValueError):
    """Raised when configuration values or files are invalid."""


AUTH_MODES: tuple[str, ...] = ("bearer", "x-api-key")

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def validate_provider_id(pid: str) -> str:
    """Validate and normalize a provider identifier."""
    if not isinstance(pid, str):
        raise ConfigError("Provider ID must be a string.")
    cleaned = pid.strip()
    if cleaned in ("anthropic", "default"):
        raise ConfigError(
            f"Provider ID '{cleaned}' is reserved for the default Anthropic account."
        )
    if not _PROVIDER_ID_RE.fullmatch(cleaned):
        raise ConfigError(
            "Provider ID must match ^[a-z0-9][a-z0-9_-]{0,47}$ (lowercase alphanumeric, '-', '_')."
        )
    return cleaned


def validate_env_var_name(name: str) -> str:
    """Validate an environment variable name and reject raw secret values."""
    if not isinstance(name, str):
        raise ConfigError("Environment variable name must be a string.")
    if not name or any(ch.isspace() for ch in name):
        raise ConfigError("Environment variable name must be non-empty without whitespace.")
    if name.lower().startswith("sk-"):
        raise ConfigError(
            "Refusing raw API key; provide an environment variable name (e.g. MY_API_KEY) instead."
        )
    if not _ENV_VAR_RE.fullmatch(name):
        raise ConfigError(
            "Environment variable name must match ^[A-Za-z_][A-Za-z0-9_]*$ and not be a raw secret."
        )
    return name


def validate_base_url(url: str) -> str:
    """Validate an HTTP(S) provider base URL and strip trailing slashes."""
    if not isinstance(url, str):
        raise ConfigError("Base URL must be a string.")
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ConfigError("Base URL must be a non-empty URL without whitespace.")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("Base URL must use http:// or https:// with a valid host.")
    return cleaned.rstrip("/")


def _validate_auth_mode(mode: str) -> str:
    if not isinstance(mode, str) or mode not in AUTH_MODES:
        raise ConfigError(f"Auth mode must be one of {AUTH_MODES}.")
    return mode


@dataclass
class ProviderConfig:
    """Configuration for a single third-party or proxy Claude provider."""

    id: str
    display_name: str
    base_url: str
    auth_mode: str = "bearer"
    auth_env: str = ""
    models_endpoint: str = "/v1/models"
    default_model: str = ""
    model_overrides: dict[str, str] = field(default_factory=dict)
    cached_models: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = validate_provider_id(self.id)
        self.display_name = (self.display_name or "").strip() or self.id
        self.base_url = validate_base_url(self.base_url)
        self.auth_mode = _validate_auth_mode(self.auth_mode)
        if self.auth_env:
            self.auth_env = validate_env_var_name(self.auth_env)
        self.models_endpoint = (self.models_endpoint or "").strip() or "/v1/models"
        self.default_model = (self.default_model or "").strip()

        cleaned_overrides: dict[str, str] = {}
        for k, v in (self.model_overrides or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                ks, vs = k.strip(), v.strip()
                if ks and vs:
                    cleaned_overrides[ks] = vs
        self.model_overrides = cleaned_overrides

        seen: set[str] = set()
        cleaned_models: list[str] = []
        for m in self.cached_models or []:
            if isinstance(m, str):
                ms = m.strip()
                if ms and ms not in seen:
                    seen.add(ms)
                    cleaned_models.append(ms)
        self.cached_models = cleaned_models


@dataclass
class AppConfig:
    """Top-level szSwap configuration."""

    schema_version: int = 1
    default_provider: str = ""
    share_projects_history: bool = True
    providers: dict[str, ProviderConfig] = field(default_factory=dict)


def get_szswap_dir() -> Path:
    """Return the szSwap home directory (SZSWAP_HOME or ~/.szswap)."""
    override = os.environ.get("SZSWAP_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".szswap"


def get_config_path() -> Path:
    """Return the default path to config.toml."""
    return get_szswap_dir() / "config.toml"


def get_claude_dir() -> Path:
    """Return the Claude Code config directory (SZSWAP_CLAUDE_DIR or ~/.claude)."""
    override = os.environ.get("SZSWAP_CLAUDE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def _resolve_windows_registry_env(var_name: str) -> str | None:
    """Fallback lookup in Windows HKCU and HKLM Environment registry keys."""
    import winreg

    hives = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for hive, subkey in hives:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, reg_type = winreg.QueryValueEx(key, var_name)
        except OSError:
            continue
        if not isinstance(value, str) or not value:
            continue
        if reg_type == winreg.REG_EXPAND_SZ:
            expand = getattr(winreg, "ExpandEnvironmentStrings", None)
            if callable(expand):
                value = expand(value)
            else:
                value = os.path.expandvars(value)
        if value:
            return value
    return None


def resolve_env_var(var_name: str) -> str | None:
    """Resolve an environment variable from process env, with Windows registry fallback."""
    if not isinstance(var_name, str) or not _ENV_VAR_RE.fullmatch(var_name):
        return None
    val = os.environ.get(var_name)
    if val:
        return val
    if sys.platform == "win32":
        return _resolve_windows_registry_env(var_name)
    return None


def check_env_status(var_name: str) -> tuple[bool, int]:
    """Return (is_set, value_length) without exposing the secret value."""
    val = resolve_env_var(var_name)
    if val:
        return True, len(val)
    return False, 0


def _provider_from_dict(pid: str, raw: dict[str, Any]) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"Provider '{pid}' entry must be a table.")
    return ProviderConfig(
        id=str(raw.get("id") or pid),
        display_name=str(raw.get("display_name") or pid),
        base_url=str(raw.get("base_url") or ""),
        auth_mode=str(raw.get("auth_mode") or "bearer"),
        auth_env=str(raw.get("auth_env") or ""),
        models_endpoint=str(raw.get("models_endpoint") or "/v1/models"),
        default_model=str(raw.get("default_model") or ""),
        model_overrides=dict(raw.get("model_overrides") or {}),
        cached_models=list(raw.get("cached_models") or []),
    )


def _provider_to_dict(provider: ProviderConfig) -> dict[str, Any]:
    return {
        "id": provider.id,
        "display_name": provider.display_name,
        "base_url": provider.base_url,
        "auth_mode": provider.auth_mode,
        "auth_env": provider.auth_env,
        "models_endpoint": provider.models_endpoint,
        "default_model": provider.default_model,
        "model_overrides": dict(provider.model_overrides),
        "cached_models": list(provider.cached_models),
    }


def load_config(path: Path | None = None, auto_seed: bool = True) -> AppConfig:
    """Load AppConfig from TOML, returning an empty config if the file does not exist."""
    _ = auto_seed
    cfg_path = path if path is not None else get_config_path()
    if not cfg_path.exists():
        return AppConfig()

    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw_text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Failed to read config '{cfg_path}': {exc}") from exc

    providers: dict[str, ProviderConfig] = {}
    raw_providers = data.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raise ConfigError("'providers' section in config must be a table.")
    for pid, pdata in raw_providers.items():
        provider = _provider_from_dict(str(pid), pdata)
        providers[provider.id] = provider

    default_provider = str(data.get("default_provider") or "").strip()
    if default_provider and default_provider not in providers:
        default_provider = ""

    return AppConfig(
        schema_version=int(data.get("schema_version", 1)),
        default_provider=default_provider,
        share_projects_history=bool(data.get("share_projects_history", True)),
        providers=providers,
    )


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    """Atomically write AppConfig to TOML using a temp file and os.replace."""
    cfg_path = path if path is not None else get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    serialized_providers: dict[str, dict[str, Any]] = {}
    for pid, provider in cfg.providers.items():
        # Re-validate critical fields before writing to disk.
        validated_id = validate_provider_id(provider.id)
        if pid != validated_id:
            raise ConfigError(f"Provider key '{pid}' does not match provider.id '{validated_id}'.")
        validate_base_url(provider.base_url)
        _validate_auth_mode(provider.auth_mode)
        if provider.auth_env:
            validate_env_var_name(provider.auth_env)
        serialized_providers[validated_id] = _provider_to_dict(provider)

    payload: dict[str, Any] = {
        "schema_version": int(cfg.schema_version),
        "default_provider": str(cfg.default_provider or ""),
        "share_projects_history": bool(cfg.share_projects_history),
        "providers": serialized_providers,
    }
    toml_text = tomli_w.dumps(payload)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cfg_path.parent,
            prefix=".config.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = handle.name
            handle.write(toml_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cfg_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return cfg_path
