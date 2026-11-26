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

- Python 3.12+ (also required when installing through npm/bun)
- Claude Code CLI (`claude`) available on `PATH`

## Installation

szSwap is a Python program. Any of these installers give you the same `szswap`
command — pick whatever you already use.

### Python installers

```bash
uv tool install .                                       # from a checkout
uv tool install git+https://github.com/seiz888/szswap   # from git
pipx install .                                          # or from git
pip install --user .                                    # plain pip
```

After a plain `pip install` you can also run it as a module:

```bash
python -m szswap status
```

### npm / bun

```bash
npm install -g .        # from a checkout
bun add -g .
npx szswap --help       # without installing globally
bunx szswap --help
```

The npm/bun package is a thin wrapper around the Python program: it creates a
private virtualenv at `~/.szswap/venv` (override with `SZSWAP_VENV`), installs
the Python sources into it — using `uv` when available, `pip` otherwise — and
reuses it for every later run. Nothing runs at install time; the first `szswap`
invocation performs the one-time setup, so `postinstall` restrictions (bun
blocks them by default) and npm's git-install lifecycle bug do not matter.

### From source (development)

```bash
uv sync --extra dev
uv run szswap
# or
pip install -e ".[dev]"
pytest -q
```

Packaging entry points:

```bash
uv build                     # dist/szswap-<version>.tar.gz + .whl
npm pack                     # szswap-<version>.tgz for the npm/bun wrapper
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
| `szswap run <provider> [--dir PATH] [-- <claude_args>...]` | Launch an isolated Claude Code session for `<provider>`; unrecognized flags are forwarded to `claude` |
| `szswap use <provider\|off\|anthropic\|default>` | Toggle Hot-Swap in `~/.claude/settings.json`; `off` restores the values that existed before |
| `szswap status` | Show current Hot-Swap status, Claude account, and configured providers |
| `szswap fetch <provider>` | Fetch and cache models from the provider's models endpoint |
| `szswap providers` | List configured providers and model overrides |

## Configuration

Configuration is stored in `~/.szswap/config.toml` (override directory with `SZSWAP_HOME`; override Claude Code config directory with `SZSWAP_CLAUDE_DIR`).

```toml
schema_version = 1
default_provider = "mygw"
share_projects_history = true

# Optional: extra Claude model IDs always shown in the override matrix,
# on top of the built-in fallback list (see src/szswap/catalog.py).
claude_model_slots = ["claude-opus-5-5", "claude-sonnet-5-5"]

[providers.mygw]
id = "mygw"
display_name = "My Gateway"
base_url = "https://api.example.com"
auth_mode = "bearer" # "bearer" or "x-api-key"
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
- **Mode B (`szswap use`)** writes the resolved token into `~/.claude/settings.json` (`env.ANTHROPIC_AUTH_TOKEN` or `env.ANTHROPIC_API_KEY`) as required by Claude Code's settings file, and sets the file mode to `0600`.
- Before the first Hot-Swap, the previous `env` / `modelOverrides` values are snapshotted to `~/.szswap/state.json` (mode `0600`; it may contain a token you configured yourself). `szswap use off` restores that snapshot exactly, so a key or override you configured yourself is never destroyed. The snapshot is deleted once restored.
- Invalid provider entries in `config.toml` are skipped with a warning instead of making the whole configuration unreadable.

## Development

```bash
uv sync --extra dev
pytest -q
```

## Releasing

The version in `pyproject.toml` and `package.json` must match the tag.

```bash
# bump both version fields, then:
git tag v0.1.0 && git push origin v0.1.0
```

`.github/workflows/release.yml` builds the wheel/sdist and publishes them to
PyPI (trusted publishing, `release` environment) and the npm wrapper to npm.
Setup required for the first release:

- PyPI: add a trusted publisher for this repo with environment `release`.
- npm: add an `NPM_TOKEN` repository secret.
- Also bump `version` in `package.json` — the workflow fails if they diverge.

## License

[MIT](LICENSE)
