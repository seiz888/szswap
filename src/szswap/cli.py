"""CLI entrypoint for szSwap."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from szswap.config import (
    AppConfig,
    ConfigError,
    ProviderConfig,
    check_env_status,
    load_config,
    save_config,
)
from szswap.engine import (
    EngineError,
    apply_hotswap,
    get_status,
    restore_anthropic,
    run_isolated,
)
from szswap.fetcher import FetchError, fetch_models
from szswap.tui import LaunchRequest, SzSwapApp


def _require_provider(cfg: AppConfig, provider_id: str) -> ProviderConfig:
    provider = cfg.providers.get(provider_id)
    if provider is None:
        available = ", ".join(sorted(cfg.providers.keys())) or "(none)"
        raise ConfigError(f"Unknown provider '{provider_id}'. Available: {available}")
    return provider


def _format_env_badge(var_name: str) -> str:
    is_set, length = check_env_status(var_name)
    return f"SET (len={length})" if is_set else "NOT SET"


def _cmd_ui() -> int:
    result = SzSwapApp().run()
    if isinstance(result, LaunchRequest):
        cfg = load_config()
        provider = _require_provider(cfg, result.provider_id)
        return run_isolated(provider, cfg, cwd=result.cwd)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    provider = _require_provider(cfg, args.provider)
    claude_args = list(getattr(args, "claude_args", None) or [])
    cwd = Path(args.dir).expanduser() if args.dir else None
    return run_isolated(provider, cfg, cwd=cwd, claude_args=claude_args)


def _cmd_use(args: argparse.Namespace) -> int:
    target = args.target.strip()
    if target.lower() in {"off", "anthropic", "default"}:
        settings_path = restore_anthropic()
        if settings_path.exists():
            print(f"Hot-Swap OFF (Anthropic Direct). Restored: {settings_path}")
        else:
            print("Hot-Swap OFF (Anthropic Direct). No settings.json found; nothing to restore.")
        return 0

    cfg = load_config()
    provider = _require_provider(cfg, target)
    settings_path = apply_hotswap(provider)
    print(f"Hot-Swap ON: '{provider.id}' ({provider.display_name}).")
    print(f"  Base URL:  {provider.base_url}")
    print(f"  Auth:      {provider.auth_mode} via {provider.auth_env} [{_format_env_badge(provider.auth_env)}]")
    print(f"  Overrides: {len(provider.model_overrides)}")
    print(f"  Updated:   {settings_path}")
    return 0


def _cmd_status() -> int:
    cfg = load_config()
    status = get_status(cfg)

    if status.mode == "anthropic":
        print("Hot-Swap:       OFF (Anthropic Direct)")
    else:
        matched = status.matched_provider_id or "custom"
        print(f"Hot-Swap:       ON ({matched}) -> {status.base_url}")
    print(f"Telemetry:      {'nonessential disabled' if status.nonessential_disabled else 'default'}")
    print(f"Claude Account: {status.account_email or 'not detected'}")
    print(f"Model:          {status.claude_default_model or '(default)'}")

    if status.model_overrides:
        print("Active Model Overrides:")
        for src, dst in sorted(status.model_overrides.items()):
            print(f"  {src} -> {dst}")
    else:
        print("Active Model Overrides: (none)")

    print("\nConfigured Providers:")
    if not cfg.providers:
        print("  (none)")
    else:
        for pid in sorted(cfg.providers.keys()):
            p = cfg.providers[pid]
            badge = _format_env_badge(p.auth_env)
            print(
                f"  - {p.id} ({p.display_name}): {p.base_url} "
                f"[{p.auth_env}: {badge}] ({len(p.model_overrides)} overrides)"
            )
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    cfg = load_config()
    provider = _require_provider(cfg, args.provider)
    result = fetch_models(
        base_url=provider.base_url,
        auth_mode=provider.auth_mode,
        auth_env=provider.auth_env,
        models_endpoint=provider.models_endpoint,
    )
    provider.cached_models = list(result.models)
    save_config(cfg)

    print(f"Fetched {len(result.models)} models for '{provider.id}' from {result.endpoint_used} (HTTP {result.status_code}).")
    sample = result.models[:10]
    for model_id in sample:
        print(f"  - {model_id}")
    if len(result.models) > len(sample):
        print(f"  ... (+{len(result.models) - len(sample)} more)")
    return 0


def _cmd_providers() -> int:
    cfg = load_config()
    if not cfg.providers:
        print("No providers configured. Run 'szswap' to add one.")
        return 0

    for pid in sorted(cfg.providers.keys()):
        p = cfg.providers[pid]
        badge = _format_env_badge(p.auth_env)
        print(f"{p.id} ({p.display_name})")
        print(f"  Base URL:      {p.base_url}")
        print(f"  Auth:          {p.auth_mode} via {p.auth_env} [{badge}]")
        print(f"  Cached Models: {len(p.cached_models)}")
        if p.model_overrides:
            print("  Model Overrides:")
            for src, dst in sorted(p.model_overrides.items()):
                print(f"    {src} -> {dst}")
        else:
            print("  Model Overrides: (none)")
    return 0


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("provider", help="Provider ID to launch")
    parser.add_argument(
        "--dir",
        dest="dir",
        default=None,
        help="Working directory for Claude Code (defaults to the current directory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="szswap",
        description="Safe multi-provider & model-override switcher for Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ui", help="Launch the interactive TUI (default)")

    p_run = sub.add_parser("run", help="Launch an isolated Claude Code session for a provider")
    _add_run_arguments(p_run)

    p_use = sub.add_parser(
        "use",
        help="Toggle Hot-Swap (~/.claude/settings.json) to a provider, or 'off' / 'anthropic' / 'default'",
    )
    p_use.add_argument("target", help="Provider ID, 'off', 'anthropic', or 'default'")

    sub.add_parser("status", help="Show Hot-Swap status, Claude account, and configured providers")
    p_fetch = sub.add_parser("fetch", help="Fetch and cache available models for a provider")
    p_fetch.add_argument("provider", help="Provider ID")

    sub.add_parser("providers", help="List configured providers and model overrides")

    return parser


def _parse_run_argv(argv: list[str]) -> argparse.Namespace:
    """Parse `run` separately so every non-szswap flag is forwarded to claude verbatim.

    argparse's REMAINDER (and parse_known_args ordering) would otherwise swallow
    szswap's own `--dir` option or drop non-flag arguments out of order.
    """
    run_parser = argparse.ArgumentParser(
        prog="szswap run",
        description="Launch an isolated Claude Code session for a provider. "
        "Any unrecognized argument is forwarded to claude.",
    )
    _add_run_arguments(run_parser)
    args, rest = run_parser.parse_known_args(argv)
    args.command = "run"
    args.claude_args = rest
    return args


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv at the first bare '--' so claude args can never be eaten by argparse."""
    if "--" not in argv:
        return argv, []
    split_at = argv.index("--")
    return argv[:split_at], argv[split_at + 1 :]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser_argv, passthrough = _split_passthrough(raw)

    if parser_argv[:1] == ["run"]:
        try:
            args = _parse_run_argv(parser_argv[1:])
        except SystemExit as exc:
            return int(exc.code or 0)
        args.claude_args = passthrough + list(args.claude_args or [])
        unknown: list[str] = []
    else:
        parser = build_parser()
        args, unknown = parser.parse_known_args(parser_argv)

    if args.command != "run" and unknown:
        print(f"error: unrecognized arguments: {' '.join(unknown)}", file=sys.stderr)
        return 2

    try:
        if args.command is None or args.command == "ui":
            return _cmd_ui()
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "use":
            return _cmd_use(args)
        if args.command == "status":
            return _cmd_status()
        if args.command == "fetch":
            return _cmd_fetch(args)
        if args.command == "providers":
            return _cmd_providers()
        print(f"error: unknown command '{args.command}'", file=sys.stderr)
        return 2
    except (ConfigError, FetchError, EngineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
