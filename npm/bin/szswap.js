#!/usr/bin/env node
"use strict";

/**
 * npm/bun entrypoint: delegate to the real Python CLI inside the private venv.
 */

const { spawn } = require("node:child_process");
const { ensureVenv } = require("../lib/bootstrap");

let python;
try {
  python = ensureVenv();
} catch (err) {
  console.error(`szswap: could not prepare its Python environment: ${err.message}`);
  process.exit(1);
}

const child = spawn(python, ["-m", "szswap", ...process.argv.slice(2)], {
  stdio: "inherit",
  env: process.env,
});

for (const signal of ["SIGTERM", "SIGHUP"]) {
  process.on(signal, () => child.kill(signal));
}

child.on("error", (err) => {
  console.error(`szswap: ${err.message}`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code === null ? 1 : code);
});
