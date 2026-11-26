"""Runtime switching engine for Mode A (isolated profile) and Mode B (hotswap)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from szswap.config import (
    AUTH_MODES,
    AppConfig,
    ProviderConfig,
    get_claude_dir,
    get_szswap_dir,
    resolve_env_var,
)


class EngineError(RuntimeError):
    """Raised when profile preparation, launch, or hotswap operations fail."""


MANAGED_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)

_SAFE_LINK_DIRS: tuple[str, ...] = (
    "skills",
    "hooks",
    "plugins",
    "commands",
    "agents",
)

_SAFE_COPY_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "keybindings.json",
)

_SAFE_SETTINGS_KEYS: tuple[str, ...] = (
    "permissions",
    "hooks",
    "statusLine",
    "enabledPlugins",
    "skillOverrides",
    "effortLevel",
    "skipDangerousModePermissionPrompt",
    "enableWorkflows",
    "extraKnownMarketplaces",
    "model",
)


@dataclass
class RuntimeStatus:
    """Snapshot of the active Claude Code settings.json state."""

    mode: str
    base_url: str
    matched_provider_id: str | None
    model_overrides: dict[str, str]
    nonessential_disabled: bool
    account_email: str | None
    claude_default_model: str | None


@dataclass
class LaunchSpec:
    """Resolved command, environment, profile path, and cwd for Mode A launch."""

    command: list[str]
    env: dict[str, str]
    profile_dir: Path
    cwd: Path


def _require_token(provider: ProviderConfig) -> str:
    """Fail-closed lookup of the provider's API key from environment."""
    if not provider.auth_env:
        raise EngineError(f"Provider '{provider.id}' has no auth_env configured.")
    token = resolve_env_var(provider.auth_env)
    if not token:
        raise EngineError(
            f"No API key for provider '{provider.id}': set '{provider.auth_env}' in your "
            "environment or paste it in the provider editor (szswap)."
        )
    return token


def _read_json_dict(path: Path, *, strict: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise EngineError(f"Failed to parse JSON file '{path}': {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise EngineError(f"Expected JSON object in '{path}'.")
        return {}
    return data


def _write_json_atomic(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = handle.name
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # settings.json can contain a raw API token in Mode B: keep it owner-only.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(callable(isjunction) and isjunction(path))


def _remove_link_or_path(path: Path) -> None:
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction) and isjunction(path):
        os.rmdir(path)
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        try:
            path.rmdir()
        except OSError as exc:
            raise EngineError(
                f"Refusing to delete non-empty directory '{path}'. "
                "Move it aside manually, then retry."
            ) from exc


def _link_dir_idempotent(src: Path, dst: Path) -> None:
    """Link dst -> src using Windows junctions or POSIX symlinks idempotently."""
    src_resolved = src.resolve()
    if dst.exists() or _is_link_or_junction(dst):
        if _is_link_or_junction(dst):
            try:
                if dst.resolve() == src_resolved:
                    return
            except OSError:
                pass
        _remove_link_or_path(dst)

    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src_resolved)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise EngineError(
                f"Failed to create directory junction '{dst}' -> '{src_resolved}': "
                f"{(result.stderr or result.stdout).strip()}"
            )
    else:
        os.symlink(src_resolved, dst, target_is_directory=True)


def read_claude_account_readonly(claude_dir: Path | None = None) -> str | None:
    """Read oauthAccount.emailAddress from .claude.json without modifying it."""
    cdir = claude_dir if claude_dir is not None else get_claude_dir()
    candidates = [cdir / ".claude.json"]
    if cdir.name == ".claude":
        candidates.append(cdir.parent / ".claude.json")
    for candidate in candidates:
        data = _read_json_dict(candidate, strict=False)
        oauth = data.get("oauthAccount")
        if isinstance(oauth, dict):
            email = oauth.get("emailAddress")
            if isinstance(email, str) and email.strip():
                return email.strip()
    return None

def get_status(cfg: AppConfig, claude_dir: Path | None = None) -> RuntimeStatus:
    """Inspect current Claude Code settings.json and match against configured providers."""
    cdir = claude_dir if claude_dir is not None else get_claude_dir()
    settings = _read_json_dict(cdir / "settings.json", strict=False)
    env = settings.get("env")
    env_dict = env if isinstance(env, dict) else {}

    base_url = str(env_dict.get("ANTHROPIC_BASE_URL") or "").strip()

    raw_overrides = settings.get("modelOverrides")
    model_overrides: dict[str, str] = {}
    if isinstance(raw_overrides, dict):
        for k, v in raw_overrides.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                model_overrides[k.strip()] = v.strip()

    nonessential_disabled = (
        str(env_dict.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") or "").strip() == "1"
    )

    raw_model = settings.get("model")
    claude_default_model = (
        raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    )

    matched_provider_id: str | None = None
    if base_url:
        normalized_base = base_url.rstrip("/")
        for provider in cfg.providers.values():
            if provider.base_url.rstrip("/") == normalized_base:
                matched_provider_id = provider.id
                break

    # Only a base URL or model overrides indicate szSwap-managed Hot-Swap. A bare
    # ANTHROPIC_API_KEY/AUTH_TOKEN may simply be the user's own Claude Code config.
    mode = "proxy" if (base_url or model_overrides) else "anthropic"
    account_email = read_claude_account_readonly(cdir)

    return RuntimeStatus(
        mode=mode,
        base_url=base_url,
        matched_provider_id=matched_provider_id,
        model_overrides=model_overrides,
        nonessential_disabled=nonessential_disabled,
        account_email=account_email,
        claude_default_model=claude_default_model,
    )


def prepare_isolated_profile(
    provider: ProviderConfig,
    cfg: AppConfig,
    claude_dir: Path | None = None,
    szswap_dir: Path | None = None,
) -> Path:
    """Prepare an isolated CLAUDE_CONFIG_DIR profile for Mode A without writing secrets."""
    _require_token(provider)
    cdir = claude_dir if claude_dir is not None else get_claude_dir()
    sdir = szswap_dir if szswap_dir is not None else get_szswap_dir()

    profile_dir = sdir / "profiles" / provider.id
    profile_dir.mkdir(parents=True, exist_ok=True)

    for dirname in _SAFE_LINK_DIRS:
        src_dir = cdir / dirname
        if src_dir.is_dir():
            _link_dir_idempotent(src_dir, profile_dir / dirname)

    projects_dst = profile_dir / "projects"
    if cfg.share_projects_history:
        projects_src = cdir / "projects"
        if projects_src.is_dir():
            _link_dir_idempotent(projects_src, projects_dst)
    elif _is_link_or_junction(projects_dst):
        _remove_link_or_path(projects_dst)

    for filename in _SAFE_COPY_FILES:
        src_file = cdir / filename
        if src_file.is_file():
            shutil.copy2(src_file, profile_dir / filename)

    orig_settings = _read_json_dict(cdir / "settings.json", strict=False)
    isolated_settings: dict[str, Any] = {
        key: copy.deepcopy(orig_settings[key])
        for key in _SAFE_SETTINGS_KEYS
        if key in orig_settings
    }
    if provider.model_overrides:
        isolated_settings["modelOverrides"] = dict(provider.model_overrides)

    # Never persist raw API tokens to disk in Mode A; inject them only via process env.
    isolated_settings["env"] = {
        "ANTHROPIC_BASE_URL": provider.base_url,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }

    _write_json_atomic(profile_dir / "settings.json", isolated_settings)
    return profile_dir


def _resolve_claude_executable() -> str:
    for candidate in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    raise EngineError("Could not locate 'claude' executable on PATH.")


def _has_model_arg(args: list[str]) -> bool:
    return any(arg == "--model" or arg.startswith("--model=") for arg in args)


def build_launch_spec(
    provider: ProviderConfig,
    cfg: AppConfig,
    cwd: Path | None = None,
    claude_args: list[str] | None = None,
    claude_dir: Path | None = None,
    szswap_dir: Path | None = None,
) -> LaunchSpec:
    """Build LaunchSpec for running Claude Code in an isolated Mode A profile."""
    if provider.auth_mode not in AUTH_MODES:
        raise EngineError(f"Unsupported auth mode '{provider.auth_mode}'.")
    launch_cwd = cwd if cwd is not None else Path.cwd()
    if not launch_cwd.is_dir():
        raise EngineError(f"Working directory '{launch_cwd}' does not exist or is not a directory.")
    token = _require_token(provider)
    profile_dir = prepare_isolated_profile(
        provider=provider,
        cfg=cfg,
        claude_dir=claude_dir,
        szswap_dir=szswap_dir,
    )

    child_env = dict(os.environ)
    for key in list(child_env.keys()):
        if key.startswith("ANTHROPIC_") or key == "CLAUDE_CONFIG_DIR":
            child_env.pop(key, None)

    child_env["CLAUDE_CONFIG_DIR"] = str(profile_dir)
    child_env["ANTHROPIC_BASE_URL"] = provider.base_url
    child_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

    if provider.auth_mode == "bearer":
        child_env["ANTHROPIC_AUTH_TOKEN"] = token
        child_env.pop("ANTHROPIC_API_KEY", None)
    else:
        child_env["ANTHROPIC_API_KEY"] = token
        child_env.pop("ANTHROPIC_AUTH_TOKEN", None)

    claude_bin = _resolve_claude_executable()
    extra_args = list(claude_args) if claude_args else []
    command = [claude_bin]
    if provider.default_model and not _has_model_arg(extra_args):
        command.extend(["--model", provider.default_model])
    command.extend(extra_args)

    launch_cwd = cwd if cwd is not None else Path.cwd()
    return LaunchSpec(
        command=command,
        env=child_env,
        profile_dir=profile_dir,
        cwd=launch_cwd,
    )


def run_isolated(
    provider: ProviderConfig,
    cfg: AppConfig,
    cwd: Path | None = None,
    claude_args: list[str] | None = None,
) -> int:
    """Execute Claude Code using an isolated Mode A profile and return its exit code."""
    spec = build_launch_spec(
        provider=provider,
        cfg=cfg,
        cwd=cwd,
        claude_args=claude_args,
    )
    try:
        completed = subprocess.run(spec.command, env=spec.env, cwd=spec.cwd)
    except OSError as exc:
        raise EngineError(f"Failed to launch '{spec.command[0]}': {exc}") from exc
    return completed.returncode


# ---------------------------------------------------------------------------
# Mode B state: snapshot of the settings.json values szSwap overwrites.
# ---------------------------------------------------------------------------

_STATE_FILENAME = "state.json"
_STATE_VERSION = 1


def _state_path(szswap_dir: Path | None = None) -> Path:
    sdir = szswap_dir if szswap_dir is not None else get_szswap_dir()
    return sdir / _STATE_FILENAME


def _load_state(path: Path) -> dict[str, Any]:
    state = _read_json_dict(path, strict=False)
    if not isinstance(state.get("hotswap"), dict):
        state["hotswap"] = {}
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    hotswap = state.get("hotswap")
    if isinstance(hotswap, dict) and hotswap:
        state["version"] = _STATE_VERSION
        _write_json_atomic(path, state)
        return
    try:
        path.unlink()
    except OSError:
        pass


def _claude_dir_key(claude_dir: Path) -> str:
    try:
        return str(claude_dir.resolve())
    except OSError:
        return str(claude_dir)


def _snapshot_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Record the pre-Hot-Swap values so `use off` can restore them exactly."""
    raw_env = settings.get("env")
    env_dict: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
    saved_env: dict[str, Any] = {}
    for key in MANAGED_ENV_KEYS:
        saved_env[key] = env_dict.get(key) if key in env_dict else None

    raw_overrides = settings.get("modelOverrides")
    return {
        "saved_env": saved_env,
        "saved_has_model_overrides": "modelOverrides" in settings,
        "saved_model_overrides": raw_overrides if isinstance(raw_overrides, dict) else {},
    }


def apply_hotswap(
    provider: ProviderConfig,
    claude_dir: Path | None = None,
    szswap_dir: Path | None = None,
) -> Path:
    """Apply Mode B hotswap directly to Claude Code's settings.json atomically."""
    if provider.auth_mode not in AUTH_MODES:
        raise EngineError(f"Unsupported auth mode '{provider.auth_mode}'.")
    token = _require_token(provider)

    cdir = claude_dir if claude_dir is not None else get_claude_dir()
    settings_path = cdir / "settings.json"
    settings = _read_json_dict(settings_path, strict=True)

    # Snapshot the previous values exactly once per Claude dir, before the first
    # overwrite, so `restore_anthropic` never destroys the user's own settings.
    state_file = _state_path(szswap_dir)
    state = _load_state(state_file)
    hotswap = state["hotswap"]
    dir_key = _claude_dir_key(cdir)
    created_entry = False
    if dir_key not in hotswap:
        hotswap[dir_key] = {
            "provider_id": provider.id,
            **_snapshot_settings(settings),
        }
        created_entry = True
        _save_state(state_file, state)

    raw_env = settings.get("env")
    env_dict: dict[str, Any] = dict(raw_env) if isinstance(raw_env, dict) else {}

    env_dict["ANTHROPIC_BASE_URL"] = provider.base_url
    if provider.auth_mode == "bearer":
        env_dict["ANTHROPIC_AUTH_TOKEN"] = token
        env_dict["ANTHROPIC_API_KEY"] = ""
    else:
        env_dict["ANTHROPIC_AUTH_TOKEN"] = ""
        env_dict["ANTHROPIC_API_KEY"] = token

    env_dict["ANTHROPIC_DEFAULT_OPUS_MODEL"] = ""
    env_dict["ANTHROPIC_DEFAULT_SONNET_MODEL"] = ""
    env_dict["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = ""
    env_dict["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    settings["env"] = env_dict

    if provider.model_overrides:
        settings["modelOverrides"] = dict(provider.model_overrides)
    else:
        settings.pop("modelOverrides", None)

    try:
        written = _write_json_atomic(settings_path, settings)
    except OSError:
        if created_entry:
            hotswap.pop(dir_key, None)
            _save_state(state_file, state)
        raise

    if not created_entry and isinstance(hotswap.get(dir_key), dict):
        hotswap[dir_key]["provider_id"] = provider.id
        _save_state(state_file, state)
    return written


def restore_anthropic(
    claude_dir: Path | None = None,
    szswap_dir: Path | None = None,
) -> Path:
    """Restore the settings.json values that existed before Hot-Swap was enabled."""
    cdir = claude_dir if claude_dir is not None else get_claude_dir()
    settings_path = cdir / "settings.json"

    state_file = _state_path(szswap_dir)
    state = _load_state(state_file)
    hotswap = state["hotswap"]
    dir_key = _claude_dir_key(cdir)
    raw_entry = hotswap.get(dir_key)
    entry: dict[str, Any] | None = raw_entry if isinstance(raw_entry, dict) else None

    if not settings_path.exists():
        # Nothing to restore; never create a settings file just to write blanks.
        if entry is not None:
            hotswap.pop(dir_key, None)
            _save_state(state_file, state)
        return settings_path

    settings = _read_json_dict(settings_path, strict=True)
    raw_env = settings.get("env")
    env_dict: dict[str, Any] = dict(raw_env) if isinstance(raw_env, dict) else {}

    if entry is not None:
        saved_env = entry.get("saved_env")
        saved_env = saved_env if isinstance(saved_env, dict) else {}
        for key in MANAGED_ENV_KEYS:
            if key not in saved_env:
                continue
            previous = saved_env[key]
            if previous is None:
                env_dict.pop(key, None)
            else:
                env_dict[key] = previous
        if entry.get("saved_has_model_overrides"):
            saved_overrides = entry.get("saved_model_overrides")
            settings["modelOverrides"] = (
                dict(saved_overrides) if isinstance(saved_overrides, dict) else {}
            )
        else:
            settings.pop("modelOverrides", None)
    else:
        # Legacy install (no snapshot): fall back to clearing managed keys only.
        for key in MANAGED_ENV_KEYS:
            env_dict[key] = ""
        settings.pop("modelOverrides", None)

    settings["env"] = env_dict
    written = _write_json_atomic(settings_path, settings)

    if entry is not None:
        hotswap.pop(dir_key, None)
        _save_state(state_file, state)
    return written
