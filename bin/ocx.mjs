#!/usr/bin/env node
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { isRealBunBinary } from "../src/lib/bun-binary-validator.mjs";
import { findPathBun } from "../src/lib/bun-path-runtime.mjs";
import { scopeTranspilerCacheToRuntime } from "../src/lib/bun-transpiler-cache-scope.mjs";
import { forwardLauncherSignals } from "../src/lib/launcher-signal-forwarding.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const cliPath = join(here, "..", "src", "cli", "index.ts");
const proofPrefix = "--ocx-internal-launch-proof=";
const overrideEnv = "OPENCODEX_BUN_PATH";

try {
  process.cwd();
} catch {
  try { process.chdir(homedir()); } catch { /* Best effort for a removed working directory. */ }
}

function resolveBun() {
  const override = process.env[overrideEnv]?.trim();
  if (override) {
    const path = resolve(override);
    if (isRealBunBinary(path)) return { path, source: "override" };
    console.error(`opencodex: ${overrideEnv} does not point to a complete Bun binary; looking for the local runtime.`);
  }

  try {
    const manifest = createRequire(import.meta.url).resolve("bun/package.json");
    const { version } = JSON.parse(readFileSync(manifest, "utf8"));
    for (const name of ["bun.exe", "bun"]) {
      const path = join(dirname(manifest), "bin", name);
      if (isRealBunBinary(path)) return { path, source: "bundled", version };
    }
  } catch { /* Missing or unreadable local dependency; try PATH. */ }

  const { dependencies } = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8"));
  const found = findPathBun({ pinnedVersion: dependencies?.bun, deadlineMs: 5_000 });
  if (found) return { ...found, source: "process" };
  return null;
}

const bun = resolveBun();
if (!bun) {
  console.error("opencodex: Bun is unavailable. Install the project's dependencies with bun install, or set OPENCODEX_BUN_PATH to an installed Bun executable.");
  process.exit(1);
}

// Node sees the inherited credential slots before Bun reads project dotenv files.
const proof = randomBytes(32).toString("base64url");
const anthropicEnvSlots = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
  .filter(name => Boolean(process.env[name]));
const forwarding = forwardLauncherSignals();
const child = spawn(bun.path, [
  cliPath,
  `${proofPrefix}${proof}`,
  ...process.argv.slice(2).filter(arg => !arg.startsWith(proofPrefix)),
], {
  stdio: "inherit",
  windowsHide: true,
  env: {
    ...scopeTranspilerCacheToRuntime(process.env, bun.version, join),
    OCX_NODE_LAUNCH_CONTEXT: JSON.stringify({ version: 1, proof, anthropicEnvSlots }),
    OCX_BUN_RUNTIME_SOURCE: bun.source,
    OCX_BUN_RUNTIME_PATH: bun.path,
  },
});
forwarding.attach(child);

child.on("error", error => {
  forwarding.clear();
  console.error(`opencodex: failed to launch Bun runtime: ${error.message}`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  forwarding.clear();
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 1);
});
