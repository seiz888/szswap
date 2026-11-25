# szSwap

Safe multi-provider and model-override switcher for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Switch between third-party Anthropic-compatible gateways and your default Anthropic account from a Textual TUI or CLI without manually editing `~/.claude/settings.json`.

## Features

- **Mode A — Isolated launch (`szswap run`)**: Prepares a per-provider `CLAUDE_CONFIG_DIR` under `~/.szswap/profiles/<id>`, links/copies safe Claude Code directories and settings, and injects API credentials via process environment variables only (raw tokens are never written to disk).
- **Mode B — Hot-Swap (`szswap use`)**: Atomically updates `~/.claude/settings.json` with a provider's base URL, auth credentials, and `modelOverrides`. Restore default Anthropic settings with `szswap use off`.
- **Interactive TUI (`szswap` / `szswap ui`)**: Manage providers, fetch models, map model overrides, launch isolated sessions, or toggle Hot-Swap.
- **Model discovery (`szswap fetch`)**: Queries `<base_url>/v1/models` and caches available models per provider.
- **Env-var-only secret references**: `~/.szswap/config.toml` stores only environment variable names (`auth_env`), never raw API keys. On Windows, user/machine registry environment variables are also resolved.

## Requirements

- Python 3.12+
- Claude Code CLI (`claude`) available on `PATH`

## Installation

Using `uv`:

```bash
uv tool install .
```

Or in a local editable environment:

```bash
uv sync --extra dev
# or
pip install -e ".[dev]"
```

## Quickstart

1. Export your gateway API key in your environment:

   ```bash
   export MY_GATEWAY_API_KEY="sk-..."
   ```

2. Launch the TUI to add a provider and configure model overrides:

   ```bash
   szswap
   ```

3. Run Claude Code using an isolated profile or Hot-Swap:

   ```bash
   # Mode A: isolated CLAUDE_CONFIG_DIR, token stays in process env only
   szswap run mygw --dir /path/to/project

   # Mode B: apply directly to ~/.claude/settings.json
   szswap use mygw

   # Restore default Anthropic settings
   szswap use off
   ```

## CLI Commands

| Command | Description |
| --- | --- |
| `szswap` / `szswap ui` | Launch the interactive TUI |
| `szswap run <provider> [--dir PATH] [-- <claude_args>...]` | Launch an isolated Claude Code session for `<provider>` |
| `szswap use <provider\|off\|anthropic\|default>` | Toggle Hot-Swap in `~/.claude/settings.json` |
| `szswap status` | Show current Hot-Swap status, Claude account, and configured providers |
| `szswap fetch <provider>` | Fetch and cache models from the provider's models endpoint |
| `szswap providers` | List configured providers and model overrides |

## Configuration

Configuration is stored in `~/.szswap/config.toml` (override directory with `SZSWAP_HOME`; override Claude Code config directory with `SZSWAP_CLAUDE_DIR`).

```toml
schema_version = 1
default_provider = "mygw"
share_projects_history = true

[providers.mygw]
id = "mygw"
display_name = "My Gateway"
base_url = "https://api.example.com"
auth_mode = "bearer" # "bearer" or "api_key"
auth_env = "MY_GATEWAY_API_KEY"
models_endpoint = "/v1/models"
default_model = ""
cached_models = []

[providers.mygw.model_overrides]
"claude-opus-5-5" = "glm-5.3"
"claude-sonnet-5-5" = "deepseek-v4-pro"
```

## Security Notes

- `config.toml` only stores `auth_env` (the environment variable name), never secret values.
- **Mode A (`szswap run`)** never persists API tokens to disk.
- **Mode B (`szswap use`)** writes the resolved token into `~/.claude/settings.json` (`env.ANTHROPIC_AUTH_TOKEN` or `env.ANTHROPIC_API_KEY`) as required by Claude Code's settings file. Run `szswap use off` to clear managed keys and `modelOverrides`.

## Development

```bash
pytest -q
```

## License

[MIT](LICENSE)
