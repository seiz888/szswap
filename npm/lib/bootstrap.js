"use strict";

/**
 * Bootstrap a private Python virtualenv for the szSwap CLI.
 *
 * npm/bun users get the real Python program: the wrapper creates a venv under
 * `$SZSWAP_HOME/venv` (or `$SZSWAP_VENV`), installs this package into it, and
 * reuses it on every later run.
 */

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const PKG_ROOT = path.resolve(__dirname, "..", "..");
const PKG_VERSION = require(path.join(PKG_ROOT, "package.json")).version;
const MIN_PYTHON = [3, 12];
const MARKER_NAME = ".szswap-installed";
const VERSION_CHECK =
  "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)";

function run(cmd, args, options) {
  return spawnSync(cmd, args, { stdio: "inherit", ...options });
}

function step(cmd, args, options) {
  const result = run(cmd, args, options);
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`'${cmd} ${args.join(" ")}' failed with exit code ${result.status}`);
  }
  return result;
}

function szswapHome() {
  const override = (process.env.SZSWAP_HOME || "").trim();
  return override ? path.resolve(override) : path.join(os.homedir(), ".szswap");
}

function venvDir() {
  const override = (process.env.SZSWAP_VENV || "").trim();
  return override ? path.resolve(override) : path.join(szswapHome(), "venv");
}

function venvPython(dir) {
  const base = dir || venvDir();
  return process.platform === "win32"
    ? path.join(base, "Scripts", "python.exe")
    : path.join(base, "bin", "python");
}

function pythonCandidates() {
  const explicit = (process.env.SZSWAP_PYTHON || "").trim();
  if (explicit) return [[explicit, []]];

  const candidates = [];
  if (process.platform === "win32") {
    candidates.push(["py", ["-3.13"]], ["py", ["-3.12"]]);
    candidates.push(["python", ["-3.13"]], ["python", ["-3.12"]], ["python", []]);
  } else {
    candidates.push(
      ["python3.14", []],
      ["python3.13", []],
      ["python3.12", []],
      ["python3", []],
    );
  }
  return candidates;
}

function findPython() {
  for (const [cmd, prefix] of pythonCandidates()) {
    const check = spawnSync(cmd, [...prefix, "-c", VERSION_CHECK], { stdio: "ignore" });
    if (check.status !== 0) continue;
    const resolved = spawnSync(
      cmd,
      [...prefix, "-c", "import sys; print(sys.executable)"],
      { encoding: "utf8" },
    );
    return resolved.status === 0 && resolved.stdout.trim()
      ? resolved.stdout.trim()
      : cmd;
  }
  return null;
}

function hasUv() {
  const probe = spawnSync("uv", ["--version"], { stdio: "ignore" });
  return probe.status === 0;
}

function markerPath(dir) {
  return path.join(dir || venvDir(), MARKER_NAME);
}

function readMarker(dir) {
  try {
    return JSON.parse(fs.readFileSync(markerPath(dir), "utf8"));
  } catch {
    return null;
  }
}

function venvUsable(dir) {
  const python = venvPython(dir);
  if (!fs.existsSync(python)) return false;
  const marker = readMarker(dir);
  if (!marker || marker.version !== PKG_VERSION) return false;
  const probe = spawnSync(python, ["-c", "import szswap"], { stdio: "ignore" });
  return probe.status === 0;
}

function createVenv(dir, python) {
  fs.mkdirSync(path.dirname(dir), { recursive: true });

  if (hasUv()) {
    step("uv", ["venv", "--python", python, "--allow-existing", dir]);
    step("uv", ["pip", "install", "--python", venvPython(dir), "--quiet", PKG_ROOT]);
    return;
  }

  step(python, ["-m", "venv", dir]);
  step(venvPython(dir), [
    "-m",
    "pip",
    "install",
    "--quiet",
    "--disable-pip-version-check",
    PKG_ROOT,
  ]);
}

/**
 * Ensure the venv exists, is up to date, and can import szswap.
 * @returns {string} absolute path to the venv python interpreter
 */
function ensureVenv({ force = false } = {}) {
  const dir = venvDir();
  if (!force && venvUsable(dir)) return venvPython(dir);

  const python = findPython();
  if (!python) {
    throw new Error(
      `Python >= ${MIN_PYTHON.join(".")} is required but was not found on PATH. ` +
        "Install it from https://www.python.org/downloads/ (or set SZSWAP_PYTHON) and retry.",
    );
  }

  createVenv(dir, python);

  // Verify before claiming success, then record the version for cheap re-checks.
  const probe = spawnSync(venvPython(dir), ["-c", "import szswap"], { stdio: "ignore" });
  if (probe.status !== 0) {
    throw new Error(
      `Python environment at '${dir}' is not usable after setup. ` +
        "Remove it and retry, or install with 'pipx install .' / 'uv tool install .'.",
    );
  }

  fs.writeFileSync(
    markerPath(dir),
    `${JSON.stringify({ version: PKG_VERSION, python, installedAt: new Date().toISOString() }, null, 2)}\n`,
    { mode: 0o600 },
  );
  return venvPython(dir);
}

module.exports = {
  PKG_ROOT,
  PKG_VERSION,
  ensureVenv,
  findPython,
  szswapHome,
  venvDir,
  venvPython,
};
