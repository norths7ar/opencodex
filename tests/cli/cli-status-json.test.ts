import { beforeAll, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync, mkdirSync, unlinkSync } from "node:fs";
import { createConnection, createServer } from "node:net";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isConnectionRefused, isUncleanExitEvidence, proxyHealthFailureReason, resolveStatusPid, selectListenTarget } from "../../backend/src/cli/status";
import * as proxyLiveness from "../../backend/src/server/proxy-liveness";
import * as startupHealth from "../../backend/src/codex/autostart-health";
import * as service from "../../backend/src/service";
import * as statusFacade from "../../backend/src/cli/status";
import * as statusProbes from "../../backend/src/cli/status-probes";
import { packageVersion } from "../../backend/src/cli/help";
import { getDefaultConfig } from "../../backend/src/config";
import { findDeadPid } from "../helpers/dead-pid";
import { COLD_SPAWN_WARMUP_HOOK_BUDGET_MS, warmColdSpawn } from "../helpers/cold-spawn-warmup";
import { removeTreeWithRetry } from "../helpers/remove-tree";
import { INTERNAL_DEADLINE_MS, SPAWN_BUDGET_MS, STORE_BUDGET_MS } from "../helpers/test-budget";
import { spawnTestChild } from "../helpers/spawn-test-child";

const repoRoot = dirname(fileURLToPath(new URL("../../package.json", import.meta.url)));
const cliPath = join(repoRoot, "backend/src", "cli", "index.ts");
const undrainedStatusHomes = new Set<string>();

function runStatusJson(opencodexHome: string) {
  return spawnSync(process.execPath, [cliPath, "status", "--json"], {
    cwd: repoRoot,
    env: { ...process.env, OPENCODEX_HOME: opencodexHome },
    encoding: "utf8",
  });
}

async function withStatusVersionFixture<T>(
  proxyVersion: string | null | undefined,
  prefix: string,
  work: (fixture: { home: string; codexHome: string }) => Promise<T>,
): Promise<T> {
  const home = mkdtempSync(join(tmpdir(), prefix));
  const codexHome = join(home, "codex");
  let server: ReturnType<typeof Bun.serve> | undefined;
  try {
    // Explicit CODEX_HOME must exist before the CLI imports codex/paths.ts.
    mkdirSync(codexHome, { recursive: true });
    server = Bun.serve({
      hostname: "127.0.0.1", port: 0,
      fetch(request) {
        return new URL(request.url).pathname === "/healthz"
          ? Response.json({ service: "opencodex", status: "ok", version: proxyVersion, uptime: 1 })
          : new Response("not found", { status: 404 });
      },
    });
    writeFileSync(join(home, "config.json"), JSON.stringify({
      ...getDefaultConfig(), port: server.port, hostname: "127.0.0.1", codexAutoStart: false,
    }));
    return await work({ home, codexHome });
  } finally {
    try {
      await server?.stop(true);
    } finally {
      if (!undrainedStatusHomes.has(home)) removeTreeWithRetry(home);
    }
  }
}

async function runTimedStatus(
  home: string,
  codexHome: string,
  json: boolean,
  deadlineMs: number,
): ReturnType<typeof spawnTestChild> {
  // Until exit and pipe closure are observed, fixture removal has no release evidence.
  undrainedStatusHomes.add(home);
  const result = await spawnTestChild([process.execPath, cliPath, "status", ...(json ? ["--json"] : [])], {
    cwd: repoRoot,
    env: { ...process.env, OPENCODEX_HOME: home, CODEX_HOME: codexHome },
    deadlineMs,
  });
  if (!result.timedOut && result.reaped && result.pipesClosed) undrainedStatusHomes.delete(home);
  return result;
}

describe("status version skew projection", () => {
  beforeAll(async () => {
    // #4948 introduced this hook for Windows alone, with a hand-derived 25s child allowance. Both
    // now come from tests/helpers/cold-spawn-warmup.ts, which derives the same 25s from the hook
    // budget it reserves teardown and reap out of. The platform gate is gone on purpose: the warm-up
    // costs one sub-second child on the POSIX lanes, and running it everywhere is what keeps the
    // mechanism exercised by every leg instead of only by the one where it was needed first.
    await warmColdSpawn("cli-index/status", async deadlineMs => {
      await withStatusVersionFixture(packageVersion(), "ocx-status-skew-cold-", async ({ home, codexHome }) => {
        const result = await runTimedStatus(home, codexHome, true, deadlineMs);
        expect(result).toMatchObject({ timedOut: false, reaped: true, pipesClosed: true });
        expect({ exitCode: result.exitCode, stderr: result.stderr }).toEqual({ exitCode: 0, stderr: "" });
        const parsed = JSON.parse(result.stdout);
        expect(parsed.schemaVersion).toBe(1);
        expect(parsed.proxy.health.ok).toBe(true);
        expect(parsed.versionSkew.proxyVersion).toBe(packageVersion());
      });
    });
  }, COLD_SPAWN_WARMUP_HOOK_BUDGET_MS);

  test.each([
    ["0.0.1", "0.0.1", "the running proxy is older", "cli-newer"],
    ["999999.0.0", "999999.0.0", "this ocx on PATH is older", "proxy-newer"],
    [packageVersion(), packageVersion(), null, "match"],
    [`${packageVersion()}+skew-fixture`, `${packageVersion()}+skew-fixture`, "neither can be identified as older", "incomparable"],
    ["not-a-version", null, null, "unknown"],
    ["unknown", null, null, "unknown"],
    ["0.0.0", "0.0.0", null, "unknown"],
    [null, null, null, "unknown"],
    [undefined, null, null, "unknown"],
    ["1.2.3\nuntrusted-health-marker", null, null, "unknown"],
    [`1.2.3+${"x".repeat(59)}`, null, null, "unknown"],
  ] as const)("projects proxy %s in JSON and human output", async (proxyVersion, projectedVersion, expected, relation) => {
    await withStatusVersionFixture(proxyVersion, "ocx-status-skew-", async ({ home, codexHome }) => {
      for (const json of [true, false]) {
        // Async child execution lets the fixture answer the real identity/health probes.
        const result = await runTimedStatus(home, codexHome, json, INTERNAL_DEADLINE_MS);
        console.log(
          `[status-version-skew] proxy=${proxyVersion ?? "undefined"} `
          + `format=${json ? "json" : "human"} elapsedMs=${result.elapsedMs.toFixed(0)}`,
        );
        expect(result).toMatchObject({ timedOut: false, reaped: true, pipesClosed: true });
        // Preserve both gates while surfacing the child error when startup fails.
        expect({ exitCode: result.exitCode, stderr: result.stderr }).toEqual({ exitCode: 0, stderr: "" });
        if (json) {
          const parsed = JSON.parse(result.stdout);
          expect(parsed.schemaVersion).toBe(1);
          expect(Object.keys(parsed.versionSkew).sort()).toEqual(["cliVersion", "proxyVersion", "relation", "skewed", "warning"]);
          expect(parsed.versionSkew.cliVersion).toBe(packageVersion());
          // Health identity carries only bounded semver; arbitrary listener text
          // must not become a version or an operator-facing skew warning.
          expect(parsed.versionSkew.proxyVersion).toBe(projectedVersion);
          expect(parsed.versionSkew.skewed).toBe(expected !== null);
          expect(parsed.versionSkew.relation).toBe(relation);
          if (expected === null) expect(parsed.versionSkew.warning).toBeNull();
          else expect(parsed.versionSkew.warning).toContain(expected);
        } else if (expected === null) {
          expect(result.stdout).not.toContain("does not match the running proxy");
        } else {
          expect(result.stdout).toContain(expected);
        }
        if (projectedVersion === null && typeof proxyVersion === "string" && proxyVersion !== "unknown") {
          expect(result.stdout).not.toContain(proxyVersion);
          expect(result.stdout).not.toContain("untrusted-health-marker");
        }
      }
      expect(existsSync(join(home, "ocx.pid"))).toBe(false);
    });
  }, SPAWN_BUDGET_MS);
});

describe("CLI status JSON", () => {
  test("status facade preserves probe identity without exposing its health helper", () => {
    expect(statusFacade.proxyHealthFailureReason).toBe(statusProbes.proxyHealthFailureReason);
    expect(statusFacade.isConnectionRefused).toBe(statusProbes.isConnectionRefused);
    expect(statusFacade.isUncleanExitEvidence).toBe(statusProbes.isUncleanExitEvidence);
    expect(statusFacade.probeUncleanExitState).toBe(statusProbes.probeUncleanExitState);
    expect(statusFacade).not.toHaveProperty("checkProxyHealth");
  });

  test("status --json prints valid read-only diagnostics without secrets", () => {
    const opencodexHome = mkdtempSync(join(tmpdir(), "ocx-status-json-"));
    try {
      const configPath = join(opencodexHome, "config.json");
      writeFileSync(configPath, JSON.stringify({
        port: 9,
        providers: {
          openai: {
            adapter: "openai-responses",
            baseUrl: "https://chatgpt.com/backend-api/codex",
            authMode: "forward",
            apiKey: "sk-test-secret",
          },
        },
        defaultProvider: "openai",
        codexAutoStart: false,
      }), "utf8");

      const beforeFiles = readdirSync(opencodexHome).sort();
      const result = runStatusJson(opencodexHome);
      const afterFiles = readdirSync(opencodexHome).sort();

      expect(result.status).toBe(0);
      expect(result.stderr).toBe("");
      expect(afterFiles).toEqual(beforeFiles);
      expect(existsSync(join(opencodexHome, "ocx.pid"))).toBe(false);

      const parsed = JSON.parse(result.stdout) as {
        schemaVersion?: unknown;
        startupSource?: unknown;
        proxy?: { running?: unknown; pid?: unknown; health?: { ok?: unknown; url?: unknown; message?: unknown } };
        dashboard?: { url?: unknown };
        listen?: { port?: unknown; source?: unknown };
        paths?: { config?: unknown; pid?: unknown; runtime?: unknown };
        runtime?: { source?: unknown };
        codexAutostart?: unknown;
        startup?: {
          status?: unknown;
          rebootSafe?: unknown;
          routingInjected?: unknown;
          serviceInstalled?: unknown;
          shimInstalled?: unknown;
          shimHealthy?: unknown;
          shimCoverage?: unknown;
          serviceSupported?: unknown;
          commands?: unknown;
        };
        defaultProvider?: unknown;
        config?: { source?: unknown; error?: unknown };
        connection?: {
          state?: unknown;
          serverUrl?: unknown;
          apiKeyId?: unknown;
          credentialFile?: unknown;
          catalog?: unknown;
        };
        service?: { summary?: unknown };
        codexShim?: { summary?: unknown };
        codexRuntime?: {
          path?: unknown;
          version?: unknown;
          source?: unknown;
          warning?: unknown;
          newerAvailable?: unknown;
          catalogClamp?: { active?: unknown; removedEfforts?: unknown; runtimeVersion?: unknown };
        };
        codexHome?: {
          effectiveCodexHome?: unknown;
          appCodexHome?: unknown;
          mismatch?: unknown;
          warning?: unknown;
          action?: unknown;
        };
      };

      expect(parsed.schemaVersion).toBe(1);
      expect(parsed.startupSource).toBe("local");
      expect(parsed.proxy?.running).toBe(false);
      expect(parsed.proxy?.pid).toBeNull();
      expect(parsed.proxy?.health?.ok).toBe(false);
      expect(parsed.proxy?.health?.url).toBe("http://127.0.0.1:9/healthz");
      expect(typeof parsed.proxy?.health?.message).toBe("string");
      expect(parsed.dashboard?.url).toBe("http://localhost:9/");
      expect(parsed.listen?.port).toBe(9);
      expect(parsed.listen?.source).toBe("config");
      expect(parsed.paths?.config).toBe(configPath);
      expect(parsed.paths?.pid).toBe(join(opencodexHome, "ocx.pid"));
      expect(typeof parsed.paths?.runtime).toBe("string");
      expect(typeof parsed.runtime?.source).toBe("string");
      expect(parsed.codexAutostart).toBe(false);
      expect(["native", "protected", "at-risk"]).toContain(parsed.startup?.status);
      expect(typeof parsed.startup?.rebootSafe).toBe("boolean");
      expect(typeof parsed.startup?.routingInjected).toBe("boolean");
      expect(typeof parsed.startup?.serviceInstalled).toBe("boolean");
      expect(typeof parsed.startup?.shimInstalled).toBe("boolean");
      expect(typeof parsed.startup?.shimHealthy).toBe("boolean");
      expect(["full", "cli-only", "none"]).toContain(parsed.startup?.shimCoverage);
      expect(typeof parsed.startup?.serviceSupported).toBe("boolean");
      expect(typeof parsed.startup?.commands).toBe("object");
      expect(parsed.defaultProvider).toBe("openai");
      expect(parsed.config?.source).toBe("file");
      expect(parsed.config?.error).toBeNull();
      expect(typeof parsed.service?.summary).toBe("string");
      expect(typeof parsed.codexShim?.summary).toBe("string");
      expect(typeof parsed.codexRuntime?.path).toBe("string");
      expect(typeof parsed.codexRuntime?.source).toBe("string");
      expect(parsed.codexRuntime?.version === null || typeof parsed.codexRuntime?.version === "string").toBe(true);
      expect(parsed.codexRuntime?.warning === null || typeof parsed.codexRuntime?.warning === "string").toBe(true);
      expect(
        parsed.codexRuntime?.newerAvailable === null
        || (typeof parsed.codexRuntime?.newerAvailable === "object" && parsed.codexRuntime?.newerAvailable !== null),
      ).toBe(true);
      expect(parsed.codexRuntime?.catalogClamp?.active).toBe(false);
      expect(Array.isArray(parsed.codexRuntime?.catalogClamp?.removedEfforts)).toBe(true);
      expect(parsed.codexRuntime?.catalogClamp?.runtimeVersion).toBeNull();
      expect(typeof parsed.codexHome?.effectiveCodexHome).toBe("string");
      expect(typeof parsed.codexHome?.appCodexHome).toBe("string");
      expect(typeof parsed.codexHome?.mismatch).toBe("boolean");
      expect(parsed.codexHome?.warning === null || typeof parsed.codexHome?.warning === "string").toBe(true);

      const serialized = JSON.stringify(parsed).toLowerCase();
      for (const forbidden of ["apikey", "sk-test-secret", "token", "refreshtoken", "authorization", "email"]) {
        expect(serialized).not.toContain(forbidden);
      }
    } finally {
      removeTreeWithRetry(opencodexHome);
    }
  });

  test("status --json reports catalogClamp.runtimeVersion when clamp is active", async () => {
    const { chmodSync } = await import("node:fs");
    const { persistEffortClamp, resetCodexRuntimeResolveCacheForTests } = await import("../../backend/src/codex/runtime");
    const opencodexHome = mkdtempSync(join(tmpdir(), "ocx-status-clamp-"));
    try {
      writeFileSync(join(opencodexHome, "config.json"), JSON.stringify({
        port: 9,
        providers: {},
        defaultProvider: "openai",
      }), "utf8");
      const fakeCodex = process.platform === "win32"
        ? join(opencodexHome, "bin", "codex.cmd")
        : join(opencodexHome, "bin", "codex");
      mkdirSync(join(opencodexHome, "bin"), { recursive: true });
      if (process.platform === "win32") {
        writeFileSync(fakeCodex, "@echo off\r\necho codex-cli 0.133.0\r\n", "utf8");
      } else {
        writeFileSync(fakeCodex, "#!/bin/sh\necho 'codex-cli 0.133.0'\n", "utf8");
        chmodSync(fakeCodex, 0o755);
      }
      persistEffortClamp({
        runtimePath: fakeCodex,
        runtimeVersion: "0.133.0",
        removedEfforts: ["xhigh"],
        affectedModels: ["gpt-5.6-sol"],
      }, { configDir: opencodexHome });
      resetCodexRuntimeResolveCacheForTests();

      const result = spawnSync(process.execPath, [cliPath, "status", "--json"], {
        cwd: repoRoot,
        env: {
          ...process.env,
          OPENCODEX_HOME: opencodexHome,
          CODEX_CLI_PATH: fakeCodex,
          PATH: "",
        },
        encoding: "utf8",
      });
      expect(result.status).toBe(0);
      const parsed = JSON.parse(result.stdout) as {
        codexRuntime?: {
          version?: string | null;
          catalogClamp?: { active?: boolean; removedEfforts?: string[]; runtimeVersion?: string | null };
        };
      };
      expect(parsed.codexRuntime?.version).toBe("0.133.0");
      expect(parsed.codexRuntime?.catalogClamp).toEqual({
        active: true,
        removedEfforts: ["xhigh"],
        runtimeVersion: "0.133.0",
      });
    } finally {
      resetCodexRuntimeResolveCacheForTests();
      removeTreeWithRetry(opencodexHome);
    }
  });

  test("status rejects unknown flags instead of silently printing human text", () => {
    const opencodexHome = mkdtempSync(join(tmpdir(), "ocx-status-json-"));
    try {
      writeFileSync(join(opencodexHome, "config.json"), JSON.stringify({
        port: 9,
        providers: {},
        defaultProvider: "openai",
      }), "utf8");

      const result = spawnSync(process.execPath, [cliPath, "status", "--yaml"], {
        cwd: repoRoot,
        env: { ...process.env, OPENCODEX_HOME: opencodexHome },
        encoding: "utf8",
      });

      expect(result.status).toBe(1);
      expect(result.stderr).toContain("Usage: ocx status [--json]");
      expect(result.stdout).toBe("");
    } finally {
      removeTreeWithRetry(opencodexHome);
    }
  });

  test("status --json rejects additional flags", () => {
    const opencodexHome = mkdtempSync(join(tmpdir(), "ocx-status-json-"));
    try {
      writeFileSync(join(opencodexHome, "config.json"), JSON.stringify({
        port: 9,
        providers: {},
        defaultProvider: "openai",
      }), "utf8");

      const result = spawnSync(process.execPath, [cliPath, "status", "--json", "--yaml"], {
        cwd: repoRoot,
        env: { ...process.env, OPENCODEX_HOME: opencodexHome },
        encoding: "utf8",
      });

      expect(result.status).toBe(1);
      expect(result.stderr).toContain("Usage: ocx status [--json]");
      expect(result.stdout).toBe("");
    } finally {
      removeTreeWithRetry(opencodexHome);
    }
  });

  test("status --json on malformed config remains read-only and secret-safe", () => {
    const opencodexHome = mkdtempSync(join(tmpdir(), "ocx-status-json-"));
    try {
      const configPath = join(opencodexHome, "config.json");
      writeFileSync(configPath, '{ "apiKey": "sk-status-secret", invalid json', "utf8");
      const beforeFiles = readdirSync(opencodexHome).sort();

      const result = runStatusJson(opencodexHome);
      const afterFiles = readdirSync(opencodexHome).sort();

      expect(result.status).toBe(0);
      expect(result.stderr).toBe("");
      expect(afterFiles).toEqual(beforeFiles);
      expect(afterFiles.some(name => name.startsWith("config.json.invalid-"))).toBe(false);

      const parsed = JSON.parse(result.stdout) as {
        config?: { source?: unknown; error?: unknown };
        paths?: { config?: unknown };
      };
      expect(parsed.paths?.config).toBe(configPath);
      expect(parsed.config?.source).toBe("fallback");
      expect(parsed.config?.error).toBe("invalid_json");

      const serialized = JSON.stringify(parsed);
      expect(serialized).not.toContain("sk-status-secret");
      expect(serialized).not.toContain("apiKey");
    } finally {
      removeTreeWithRetry(opencodexHome);
    }
  });

  test("listen target prefers current runtime port metadata", () => {
    const target = selectListenTarget(
      { port: 10100, hostname: "0.0.0.0" },
      123,
      { pid: 123, port: 58195, hostname: "0.0.0.0" },
    );

    expect(target.source).toBe("runtime");
    expect(target.port).toBe(58195);
    expect(target.healthUrl).toBe("http://127.0.0.1:58195/healthz");
    expect(target.dashboardUrl).toBe("http://localhost:58195/");
  });

  test("listen target keeps the loopback dashboard URL unchanged", () => {
    const target = selectListenTarget(
      { port: 10100, hostname: "127.0.0.1" },
      null,
      null,
    );

    expect(target.dashboardUrl).toBe("http://localhost:10100/");
  });

  test("non-loopback listen target uses its configured hostname", () => {
    const target = selectListenTarget(
      { port: 10100, hostname: "100.64.0.11" },
      null,
      null,
    );

    expect(target.dashboardUrl).toBe("http://100.64.0.11:10100/");
  });

  test("resolveStatusPid preserves an authoritative null from live orphan checks", () => {
    expect(resolveStatusPid({ pid: null }, 4242)).toBeNull();
    expect(resolveStatusPid({ pid: 1111 }, 4242)).toBe(1111);
    expect(resolveStatusPid(null, 4242)).toBe(4242);
    expect(resolveStatusPid(null, null)).toBeNull();
  });

  test("classifies an aborted direct health probe as timed out", () => {
    const controller = new AbortController();
    controller.abort();
    expect(proxyHealthFailureReason(new Error("socket closed"), controller.signal)).toBe("timed out");
    const abortError = new Error("aborted");
    abortError.name = "AbortError";
    expect(proxyHealthFailureReason(abortError, new AbortController().signal)).toBe("timed out");
    expect(proxyHealthFailureReason(new Error("connection refused"), new AbortController().signal)).toBe("unreachable");
  });

  test("listen target brackets raw IPv6 hostnames in the health URL", () => {
    const target = selectListenTarget(
      { port: 10100, hostname: "::1" },
      123,
      { pid: 123, port: 58195, hostname: "::1" },
    );

    expect(target.healthUrl).toBe("http://[::1]:58195/healthz");
    expect(target.dashboardUrl).toBe("http://localhost:58195/");
  });

  test("listen target ignores stale runtime port metadata", () => {
    const target = selectListenTarget(
      { port: 10100, hostname: "127.0.0.1" },
      123,
      { pid: 999, port: 58195 },
    );

    expect(target.source).toBe("config");
    expect(target.port).toBe(10100);
    expect(target.healthUrl).toBe("http://127.0.0.1:10100/healthz");
    expect(target.dashboardUrl).toBe("http://localhost:10100/");
  });
});

/**
 * #1419: an unsupervised proxy died from a native trap and every later command said
 * only "not running". The persisted owner records are the one piece of evidence that
 * separates a crash from a proxy that was never started, and status used to discard
 * it. These cases pin the predicate, including the two false-positive shapes that a
 * naive implementation gets wrong.
 */
describe("unclean prior exit evidence", () => {
  const base = {
    live: false,
    healthOk: false,
    healthRefused: true,
    ownerPidAlive: false,
    pidRecordBefore: 4242,
    pidRecordAfter: 4242,
    runtimePidBefore: 4242,
    runtimePidAfter: 4242,
  };

  test("both records outliving a dead owner is an unclean exit", () => {
    expect(isUncleanExitEvidence(base)).toBe(true);
  });

  // Blocker 5 from the plan audit: a fixture that always writes BOTH records cannot
  // tell an AND from an OR, so each record must be sufficient on its own.
  test("a pid record alone is sufficient", () => {
    expect(isUncleanExitEvidence({
      ...base,
      runtimePidBefore: null,
      runtimePidAfter: null,
    })).toBe(true);
  });

  test("a runtime-port record alone is sufficient", () => {
    expect(isUncleanExitEvidence({
      ...base,
      pidRecordBefore: null,
      pidRecordAfter: null,
    })).toBe(true);
  });

  test("a clean home reports nothing", () => {
    expect(isUncleanExitEvidence({
      ...base,
      pidRecordBefore: null,
      pidRecordAfter: null,
      runtimePidBefore: null,
      runtimePidAfter: null,
    })).toBe(false);
  });

  test("a live proxy or a healthy probe reports nothing", () => {
    expect(isUncleanExitEvidence({ ...base, live: true })).toBe(false);
    expect(isUncleanExitEvidence({ ...base, healthOk: true })).toBe(false);
  });

  // Re-audit blocker 2: without this case the owner-alive clause is never exercised,
  // because every other fixture names a dead pid.
  test("a live owner pid is a start in progress, not a crash", () => {
    expect(isUncleanExitEvidence({ ...base, ownerPidAlive: true })).toBe(false);
  });

  // Re-audit blocker 1: `handleStart` binds the port before it publishes either
  // record, so a start caught in that window leaves both snapshots identical. Only a
  // refused connection proves nothing holds the port.
  test("a held port is not a crash even when the records look stale", () => {
    expect(isUncleanExitEvidence({ ...base, healthRefused: false })).toBe(false);
  });

  test("records published mid-probe suppress the report", () => {
    expect(isUncleanExitEvidence({ ...base, pidRecordBefore: null })).toBe(false);
    expect(isUncleanExitEvidence({ ...base, runtimePidAfter: 9999 })).toBe(false);
  });

  // Review blocker 2: `unreachable` covers every non-abort failure, including a socket
  // that is ACCEPTED and then reset — which is what an in-flight bind looks like. Only a
  // connect-phase refusal proves the port is free.
  test("only a connect-phase refusal counts as nothing listening", () => {
    const refused = Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:9"), { code: "ECONNREFUSED" });
    expect(isConnectionRefused(refused)).toBe(true);

    const nested = new Error("fetch failed", { cause: refused });
    expect(isConnectionRefused(nested)).toBe(true);

    const reset = Object.assign(new Error("socket hang up"), { code: "ECONNRESET" });
    expect(isConnectionRefused(reset)).toBe(false);

    // A message that merely mentions refusal without the errno is not evidence.
    expect(isConnectionRefused(new Error("connection refused by policy"))).toBe(false);
    expect(isConnectionRefused(undefined)).toBe(false);
  });
});

/**
 * Command-level coverage. Review found the unit tests above were satisfiable by an
 * implementation that never reported anything: replacing the returned
 * `staleProcessState` with a constant `false` left every predicate test green. These
 * drive the real CLI, so the field has to travel from disk to output.
 */
describe("status reports stale process records end to end", () => {
  const seed = (home: string, opts: { pid?: number; runtime?: boolean; port: number }): void => {
    writeFileSync(join(home, "config.json"), JSON.stringify({ port: opts.port, codexAutoStart: false }), "utf8");
    const pid = opts.pid ?? findDeadPid();
    if (opts.pid !== 0) writeFileSync(join(home, "ocx.pid"), String(pid), "utf8");
    if (opts.runtime) {
      writeFileSync(join(home, "runtime-port.json"), JSON.stringify({ pid, port: opts.port, hostname: "127.0.0.1" }), "utf8");
    }
  };

  /**
   * A port that is genuinely free: bind an ephemeral port, read it, release it. The
   * discard port 9 is conventionally unused but not guaranteed, and if anything answers
   * on it the probe is accepted rather than refused and these fixtures invert.
   */
  async function allocateFreePort(): Promise<number> {
    const probe = createServer();
    await new Promise<void>(resolve => { probe.listen(0, "127.0.0.1", () => resolve()); });
    const port = (probe.address() as AddressInfo).port;
    await new Promise<void>(resolve => { probe.close(() => resolve()); });
    return port;
  }

  /**
   * Prove the endpoint refuses right now.
   *
   * `allocateFreePort` releases the port it reports, and on a sharded runner every other
   * test binding an ephemeral port is a candidate to take it. A fixture that assumes a
   * released port is still refusing asserts against whatever happened to bind it.
   */
  async function refusesConnection(port: number): Promise<boolean> {
    return await new Promise<boolean>(resolve => {
      const socket = createConnection({ port, host: "127.0.0.1" });
      const settle = (refused: boolean): void => { socket.destroy(); resolve(refused); };
      socket.setTimeout(1_000);
      socket.once("connect", () => settle(false));
      socket.once("timeout", () => settle(false));
      socket.once("error", () => settle(true));
    });
  }
  let freePort: number;
  beforeEach(async () => { freePort = await allocateFreePort(); });

  test("a dead owner record surfaces in --json and in human output", async () => {
    const home = mkdtempSync(join(tmpdir(), "ocx-stale-json-"));
    try {
      // A released port can be claimed by another test, and a real probe can
      // time out without a refusal. Observe each CLI invocation's own verdict;
      // another process before or after it cannot certify what it saw.
      let parsed: { proxy?: { staleProcessState?: unknown } } | undefined;
      let humanStdout: string | undefined;
      for (let attempt = 0; attempt < 5 && humanStdout === undefined; attempt++) {
        const port = await allocateFreePort();
        if (!await refusesConnection(port)) continue;
        seed(home, { runtime: true, port });

        const json = runStatusJson(home);
        expect(json.status).toBe(0);
        const observed = JSON.parse(json.stdout) as { proxy?: { staleProcessState?: unknown } };
        if (!await refusesConnection(port)) continue;
        // A /healthz probe can abort without ECONNREFUSED even while the port is empty; that
        // leaves the field false without anything having taken the port. Retry rather than
        // treat a timed-out probe as a verdict.
        if (observed?.proxy?.staleProcessState !== true) continue;

        const human = spawnSync(process.execPath, [
          "--preload", join(repoRoot, "tests/helpers/status-stale-observation.ts"), cliPath, "status",
        ], {
          cwd: repoRoot,
          env: { ...process.env, OPENCODEX_HOME: home },
          encoding: "utf8",
        });
        expect(human.status).toBe(0);
        // The observer delegates to the real probe and returns its result unchanged.
        // Only this process's receipt can prove the human formatter saw stale=true.
        const receipt = human.stderr.match(/OCX_TEST_STALE_OBSERVATION=(true|false)/);
        expect(receipt, "human status must execute the actual stale-process probe").not.toBeNull();
        if (receipt?.[1] !== "true") continue;
        parsed = observed;
        humanStdout = human.stdout;
      }

      expect(
        humanStdout,
        "no allocated port stayed refused, with the stale verdict reached, across every status probe",
      ).toBeDefined();
      expect(parsed?.proxy?.staleProcessState).toBe(true);
      expect(humanStdout).toContain("may have exited unexpectedly");
    } finally {
      removeTreeWithRetry(home);
    }
  });

  test("a clean home reports false and says nothing about a previous run", () => {
    const home = mkdtempSync(join(tmpdir(), "ocx-stale-clean-"));
    try {
      writeFileSync(join(home, "config.json"), JSON.stringify({ port: freePort, codexAutoStart: false }), "utf8");

      const json = runStatusJson(home);
      const parsed = JSON.parse(json.stdout) as { proxy?: { staleProcessState?: unknown } };
      expect(parsed.proxy?.staleProcessState).toBe(false);

      const human = spawnSync(process.execPath, [cliPath, "status"], {
        cwd: repoRoot,
        env: { ...process.env, OPENCODEX_HOME: home },
        encoding: "utf8",
      });
      expect(human.stdout).not.toContain("may have exited unexpectedly");
    } finally {
      removeTreeWithRetry(home);
    }
  });

  // Review blocker 3: a recycled pid must suppress rather than assert. This process is
  // certainly alive, so recording it stands in for a reused pid.
  test("a record naming a live pid is never reported as a stale exit", () => {
    const home = mkdtempSync(join(tmpdir(), "ocx-stale-livepid-"));
    try {
      seed(home, { pid: process.pid, runtime: true, port: freePort });

      const parsed = JSON.parse(runStatusJson(home).stdout) as { proxy?: { staleProcessState?: unknown } };
      expect(parsed.proxy?.staleProcessState).toBe(false);
    } finally {
      removeTreeWithRetry(home);
    }
  });

  // Review blocker 4: status and doctor disagreed when the recorded port differed from
  // the configured one. Both now probe the recorded port, so both must agree.
  //
  // The configured port must be OCCUPIED for this to discriminate: if both ports are
  // simply free, probing either one yields the same refusal and the test cannot tell the
  // two implementations apart. A listener that accepts and resets is what an in-flight
  // bind looks like, so a run that probed the configured port would suppress the report.
  test("a fallback-port record is judged on the recorded port, not the configured one", async () => {
    const home = mkdtempSync(join(tmpdir(), "ocx-stale-fallback-"));
    const occupied = createServer(socket => { socket.destroy(); });
    await new Promise<void>(resolve => { occupied.listen(0, "127.0.0.1", () => resolve()); });
    const occupiedPort = (occupied.address() as AddressInfo).port;
    try {
      const pid = findDeadPid();
      writeFileSync(join(home, "config.json"), JSON.stringify({ port: occupiedPort, codexAutoStart: false }), "utf8");
      writeFileSync(join(home, "ocx.pid"), String(pid), "utf8");

      // The recorded port has to refuse for this to discriminate, and `allocateFreePort`
      // hands back a port it has already released. Confirm refusal immediately before and
      // immediately after the probe, and re-allocate when something took it in between, so
      // a stolen port retries instead of failing an assertion it never exercised.
      let parsed: { proxy?: { staleProcessState?: unknown } } | undefined;
      for (let attempt = 0; attempt < 5 && parsed === undefined; attempt++) {
        const recordedPort = await allocateFreePort();
        if (recordedPort === occupiedPort) continue;
        if (!await refusesConnection(recordedPort)) continue;
        writeFileSync(join(home, "runtime-port.json"), JSON.stringify({ pid, port: recordedPort, hostname: "127.0.0.1" }), "utf8");
        const observed = JSON.parse(runStatusJson(home).stdout) as { proxy?: { staleProcessState?: unknown } };
        if (!await refusesConnection(recordedPort)) continue;
        // The TCP check can refuse while the HTTP /healthz probe aborts at 800ms
        // without an ECONNREFUSED code. That leaves staleProcessState false even
        // though the recorded port is still empty; retry instead of treating a
        // timed-out probe as "judged the occupied configured port".
        if (observed?.proxy?.staleProcessState !== true) continue;
        parsed = observed;
      }

      expect(parsed, "no allocated port stayed refused across the status probe").toBeDefined();
      expect(parsed?.proxy?.staleProcessState).toBe(true);
    } finally {
      await new Promise<void>(resolve => { occupied.close(() => resolve()); });
      removeTreeWithRetry(home);
    }
  });
});


test("collectStatus keeps a viable registered service marked NOT serving without a live proxy", async () => {
  const home = mkdtempSync(join(tmpdir(), "ocx-status-service-absent-"));
  const previousHome = process.env.OPENCODEX_HOME;
  const previousCodexHome = process.env.CODEX_HOME;
  const restore: Array<() => void> = [];
  try {
    mkdirSync(join(home, "codex"));
    process.env.OPENCODEX_HOME = home;
    process.env.CODEX_HOME = join(home, "codex");
    writeFileSync(join(home, "config.json"), JSON.stringify({ ...getDefaultConfig(), port: 9 }));
    const local = startupHealth.deriveStartupHealth({ routingKind: "opencodex-local", platform: "darwin",
      autostartEnabled: true, serviceInstalled: true, serviceViable: true, serviceEnabled: true,
      serviceRunning: true, serviceStale: false, serviceConflict: false, serviceSupported: true,
      shimInstalled: false, shimHealthy: false });
    const find = spyOn(proxyLiveness, "findLiveProxy").mockResolvedValue(null);
    restore.push(() => find.mockRestore());
    const collect = spyOn(startupHealth, "collectStartupHealth").mockReturnValue(local);
    restore.push(() => collect.mockRestore());
    const diagnostic = spyOn(service, "diagnoseService").mockReturnValue({ supported: true,
      installed: true, enabled: true, running: true, viable: true, startable: true, stale: false,
      conflict: false, backend: "launchd", summary: "registered viable fixture" });
    restore.push(() => diagnostic.mockRestore());
    const result = (await statusFacade.collectStatus()).json;
    expect(result.proxy.running).toBe(false);
    expect(result.startupSource).toBe("local");
    expect(result.startup.protection).toBe("service");
    expect(result.service.summary).toContain("registered viable fixture — registered but NOT serving");
    expect(result.service.summary).not.toContain("running under the live managed service");
  } finally {
    for (const cleanup of restore.reverse()) cleanup();
    if (previousHome === undefined) delete process.env.OPENCODEX_HOME;
    else process.env.OPENCODEX_HOME = previousHome;
    if (previousCodexHome === undefined) delete process.env.CODEX_HOME;
    else process.env.CODEX_HOME = previousCodexHome;
    removeTreeWithRetry(home);
  }
}, STORE_BUDGET_MS);
