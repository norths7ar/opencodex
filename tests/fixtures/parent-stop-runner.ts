/** Runs the real CLI/stop module graph with process and client I/O isolated in this child. */
import { mock } from "bun:test";
import * as childProcess from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { CodexNativeRestoreResult } from "../../src/codex/inject";
import { createLocalAttestationProof, LOCAL_ATTESTATION_CHALLENGE_HEADER, LOCAL_ATTESTATION_PROOF_HEADER } from "../../src/lib/local-management-attestation";
import { LOCAL_MANAGEMENT_NONCE_HEADER } from "../../src/lib/local-management-capability";

const ATTESTATION_SECRET = "C".repeat(43);

const options = JSON.parse(readFileSync(0, "utf8")) as {
  receipt: boolean;
  response: unknown;
  restore: CodexNativeRestoreResult;
  status?: number;
  serverTeardown?: boolean;
  json?: boolean;
  managerCascades?: boolean;
  orphan?: boolean;
  exitCleanup?: boolean;
  nativeSignal?: boolean;
  apiFailure?: boolean;
  stopBodyFailure?: boolean;
  proxyProbePort?: number;
  alreadyStopped?: boolean;
  absence?: "dead" | "unknown" | "live";
  managerState?: "inactive" | "active" | "unknown";
  receiptClearBusy?: boolean;
  /** The runtime-port record is missing, so the stop can only record a guessed endpoint. */
  noRuntimeRecord?: boolean;
  /** Like the real readPid, report no PID once the process is gone (a manager stop exited it). */
  deadPidReadsNull?: boolean;
};
const home = process.env.OPENCODEX_HOME!;
const endpoint = { hostname: "127.0.0.1", port: 10100 };
const fakePid = 4242;
const calls = { killed: 0, native: 0, grok: 0, cleared: 0, exited: 0, service: 0, lifecycle: 0 };
let receiptBlocker: import("bun:sqlite").Database | undefined;
const { Database } = await import("bun:sqlite");
const { prepareConfigMutationDatabasePathForWrite } = await import("../../src/config");
const urls: string[] = [];
const probes: string[] = [];
const transports: string[] = [];
let viaDirect = false;
const unexpectedIo: string[] = [];
let alive = !options.alreadyStopped;
let nonce: string | undefined;

function unexpected(operation: string): never {
  unexpectedIo.push(operation);
  throw new Error(`unexpected external I/O in parent stop fixture: ${operation}`);
}

// Neither an accidental POSIX signal nor the Windows taskkill fallback may reach the host.
process.kill = ((pid: number, signal?: number | NodeJS.Signals) => {
  if (options.nativeSignal && pid === fakePid && signal === "SIGTERM") { calls.killed++; alive = false; return true; }
  if (pid !== fakePid || signal !== 0) {
    calls.killed += 1;
    return unexpected(`process.kill(${pid}, ${signal})`);
  }
  if (alive) return true;
  calls.exited += 1;
  throw Object.assign(new Error("fixture process exited"), { code: "ESRCH" });
}) as typeof process.kill;
mock.module("node:child_process", () => ({
  ...childProcess,
  execFileSync: (file: string) => {
    if (options.nativeSignal && file.endsWith("taskkill.exe")) { calls.killed++; alive = false; return Buffer.alloc(0); }
    calls.killed += 1; return unexpected("execFileSync");
  },
}));

const receipts = await import("../../src/config/pending-teardown");
process.on("exit", () => {
  receiptBlocker?.close();
  writeFileSync(join(home, "parent-stop-result.json"), JSON.stringify({
    calls, urls, probes, transports, unexpectedIo, nonce,
    receiptExists: nonce !== undefined && existsSync(receipts.pendingTeardownPathFor(nonce)),
  }));
});
const claimReceipt = receipts.claimPendingTeardown;
const clearReceipt = receipts.clearPendingTeardown;
mock.module("../../src/config/pending-teardown", () => ({
  ...receipts,
  claimPendingTeardown: (...args: Parameters<typeof claimReceipt>) => {
    if (!options.receipt) throw new Error("receipt storage unavailable");
    const receipt = claimReceipt(...args);
    nonce = receipt.nonce;
    return receipt;
  },
  clearPendingTeardown: (value: string) => { calls.cleared += 1; return clearReceipt(value); },
}));

const state = await import("../../src/config/process-state");
mock.module("../../src/config/process-state", () => ({
  ...state,
  readPid: () => options.orphan || options.alreadyStopped || (options.deadPidReadsNull && !alive) ? null : fakePid,
  readRuntimePort: () => options.alreadyStopped || options.noRuntimeRecord ? null : { ...endpoint, pid: fakePid, attestationSecret: ATTESTATION_SECRET },
  removePid() {},
  removeRuntimePort() {},
}));
const service = await import("../../src/service");
mock.module("../../src/service", () => ({ ...service, stopServiceIfInstalledDetailed: () => {
  calls.service++;
  if (options.managerCascades) { alive = false; return "stopped"; }
  return "absent";
} }));
if (options.orphan || options.alreadyStopped) {
  const liveness = await import("../../src/server/proxy-liveness");
  mock.module("../../src/server/proxy-liveness", () => ({ ...liveness,
    findLiveProxy: async () => {
      return alive ? { ...endpoint, pid: fakePid, source: "config" } : null;
    },
    proveLiveProxyOwnedByHome: async () => "proven",
    probeEndpointLiveness: async () => options.absence ?? "dead",
  }));
}
if (options.alreadyStopped) {
  const guarded = await import("../../src/service/guarded-manager-target");
  mock.module("../../src/service/guarded-manager-target", () => ({ ...guarded,
    observeGuardedManagerStopped: async () => options.managerState ?? "inactive",
  }));
}
const native = await import("../../src/codex/inject");
mock.module("../../src/codex/inject", () => ({
  ...native,
  restoreNativeCodexAsync: async (restoreOptions?: { withWriteGuard?: <T>(write: () => T) => T }) => {
    await Promise.resolve(); // The real restore awaits config-file/N acquisition before committing.
    const write = () => { calls.native += 1; return options.restore; };
    return restoreOptions?.withWriteGuard ? restoreOptions.withWriteGuard(write) : write();
  },
}));
const grok = await import("../../src/grok/inject");
mock.module("../../src/grok/inject", () => ({
  ...grok,
  stripGrokConfig: () => {
    calls.grok += 1;
    if (options.receiptClearBusy) queueMicrotask(() => {
      receiptBlocker = new Database(prepareConfigMutationDatabasePathForWrite(), { readwrite: true, create: true });
      receiptBlocker.exec("PRAGMA busy_timeout = 0; BEGIN IMMEDIATE");
    });
    return { ok: true, changed: true, message: "Grok restored" };
  },
}));
const portReclaim = await import("../../src/server/port-reclaim");
mock.module("../../src/server/port-reclaim", () => ({ ...portReclaim, reclaimListenPort: async () => {} }));
// Only shim preflight is unrelated to this stop contract; parsing and dispatch stay real.
mock.module("../../src/cli/codex-shim-autorestore", () => ({ async maybeAutoRestoreCodexShimAsync() {} }));

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = String(input);
  if (init?.method === "GET") {
    probes.push(url);
    if (init.redirect !== "error") return unexpected("redirectable compatibility probe");
    const requestHeaders = new Headers(init.headers);
    if (requestHeaders.has("x-opencodex-api-key")) return unexpected("credential on compatibility probe");
    const nonce = requestHeaders.get(url.endsWith("/healthz") ? LOCAL_ATTESTATION_CHALLENGE_HEADER : LOCAL_MANAGEMENT_NONCE_HEADER);
    const proof = nonce ? createLocalAttestationProof(ATTESTATION_SECRET, nonce, fakePid, endpoint.port) : null;
    const headers = proof ? { [LOCAL_ATTESTATION_PROOF_HEADER]: proof } : undefined;
    if (!url.endsWith("/healthz")) return unexpected("Unexpected GET " + url);
    return Response.json({ service: "opencodex", version: "2.93.0", pid: fakePid }, { headers });
  }
  if (!url.startsWith("http://127.0.0.1:10100/api/stop")) return unexpected(`fetch(${url})`);
  urls.push(url);
  transports.push(viaDirect ? "direct" : "global");
  const body = options.serverTeardown && !options.apiFailure
    ? await (await import("../../src/server/stop-teardown")).performStopTeardown(new URL(url))
    : options.response;
  if (!options.nativeSignal && (options.status ?? 200) !== 409) alive = false;
  return Response.json(body, { status: options.status ?? 200 });
}) as typeof fetch;

// Only the real proxy-environment mode above exercises TCP; other cases inject responses.
const direct = await import("../../src/server/direct-local-http");
mock.module("../../src/server/direct-local-http", () => ({ ...direct,
  directLocalHttpFetch: async (input: string | URL | Request, init?: RequestInit, io?: { onResponseStatus?: (status: number) => void }) => {
    if (!alive && String(input).endsWith("/healthz")) throw Object.assign(new Error("fixture listener closed"), { code: "ECONNREFUSED" });
    if (options.stopBodyFailure && init?.method === "POST") {
      urls.push(String(input));
      io?.onResponseStatus?.(409);
      throw new Error("fixture truncated stop refusal");
    }
    viaDirect = true;
    try {
      return globalThis.fetch(input, { ...init, method: init?.method ?? "GET", redirect: "error" });
    } finally {
      viaDirect = false;
    }
  },
}));

process.argv = [process.execPath, "ocx", "stop", ...(options.json ? ["--json"] : [])];
if (options.exitCleanup) {
  delete process.env.OCX_SERVICE;
  // Execute the actual startup exit closure, with only host/config effects replaced.
  const { repoPath } = await import("../helpers/repo-root");
  const source = readFileSync(repoPath("src", "cli", "index.ts"), "utf8");
  const from = source.indexOf("  const syncCleanup = () => {");
  const to = source.indexOf("\n  let shuttingDown", from);
  if (from < 0 || to < 0) throw new Error("startup cleanup closure missing");
  const dispatch = await import("../../src/cli/dispatch");
  const bindings = {
    catalogHealer: null, routingHealer: null, guardian: { stop() {} }, historyGuardian: null, healthSentinel: null,
    siblingStart: false, isRecyclingForExit: () => false, decideStartExitTeardown: dispatch.decideStartExitTeardown,
    isSharedTeardownDeferredOnExit: () => false,
    stopRestoreWriteGuard: (await import("../../src/cli/stop-restore-guard")).stopRestoreWriteGuard,
    removePid() {}, removeRuntimePort() {}, serviceEnvironmentOwnedHere: () => true,
    getCodexHome: () => "fixture", codexHomeIsAbsent: () => false,
    restoreNativeCodex: (restoreOptions?: { withWriteGuard?: <T>(write: () => T) => T }) => {
      const write = () => { calls.native++; return options.restore; };
      return restoreOptions?.withWriteGuard ? restoreOptions.withWriteGuard(write) : write();
    },
    stripGrokConfig: () => { calls.grok++; return { ok: true, changed: true, message: "stripped" }; },
  };
  const cleanup = new Function(...Object.keys(bindings), "let cleaned = false; let cleanupSucceeded = true;\n" + source.slice(from, to) + "\nreturn syncCleanup();");
  const succeeded = cleanup(...Object.values(bindings));
  process.exit(succeeded ? 0 : 1);
}
await import("../../src/cli/index");
