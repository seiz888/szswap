#!/usr/bin/env node
"use strict";

/**
 * npm/bun postinstall hook. Never fails the install: the bin shim retries the
 * setup on first run and surfaces the real error there.
 */

const { ensureVenv, venvDir } = require("./lib/bootstrap");

try {
  process.stderr.write("szswap: preparing a private Python environment (one-time setup)...\n");
  ensureVenv();
  process.stderr.write(`szswap: ready (venv: ${venvDir()}).\n`);
} catch (err) {
  process.stderr.write(
    `szswap: automatic setup skipped: ${err.message}\n` +
      "szswap: run 'szswap --help' once to retry, or install natively with\n" +
      "        pipx install . | uv tool install . | pip install .\n",
  );
}
