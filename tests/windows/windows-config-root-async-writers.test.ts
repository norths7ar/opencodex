import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { handleConfigCommand } from "../../backend/src/cli/config-command";
import { handleEffortCommand } from "../../backend/src/cli/effort";
import { handleModels } from "../../backend/src/cli/models";
import { handleProviderCommand } from "../../backend/src/cli/provider";
import type { OcxConfig } from "../../backend/src/types";
import * as paths from "../../backend/src/config/paths";
import * as configModule from "../../backend/src/config";
import { armClaudeCodeBaseline, claudeCodeBaselineArmed } from "../../backend/src/config/live-reconcile";
import { CliUsageError } from "../../backend/src/cli/runtime-api";
import { readCliConfigForMutation } from "../../backend/src/cli/config-mutation";
import { dispatchCommand, type CliDispatchDeps } from "../../backend/src/cli/dispatch";
import { setProviderKeychainEntryFactoryForTests } from "../../backend/src/providers/api-key-resolve";
import { resetHardenedStateForTests, setAsyncIcaclsBeltSchedulerForTests, setAsyncIcaclsRunnerForTests,
  setIcaclsRunnerForTests, setNowForTests, setPlatformForTests, windowsSecretAclReapPendingForPath,
  type IcaclsResult } from "../../backend/src/lib/windows-secret-acl";
import { setSyntheticWindowsPrincipalForTests } from "../../backend/src/lib/windows-user-principal";
import { removeTreeWithRetry } from "../helpers/remove-tree";

const success: IcaclsResult = { success: true, exitCode: 0, timedOut: false, stdout: "" };
const turn = () => new Promise<void>(done => setImmediate(done));
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}
const keys = ["OPENCODEX_HOME", "OPENCODEX_ACL_TIMEOUT_MS", "OPENCODEX_ACL_VERIFY_EXISTING"] as const;
let root: string, original: string;
let previous: (string | undefined)[];
let restores: Array<() => void>;
let advisorConfig: OcxConfig | undefined;
const fixture = () => ({ port: 10100, defaultProvider: "mock", effortCap: "low", providers: {
  mock: { adapter: "openai-chat", baseUrl: "https://mock.example/v1", models: ["test-model"] },
  second: { adapter: "openai-chat", baseUrl: "https://second.example/v1", models: ["second-model"] },
}, customModels: [{ id: "fixture-model", provider: "mock", modelId: "custom-before", addedAt: "2026-01-01T00:00:00.000Z" }] });
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "ocx-cli-writer-admission-"));
  previous = keys.map(key => process.env[key]);
  process.env.OPENCODEX_HOME = root; process.env.OPENCODEX_ACL_TIMEOUT_MS = "1000";
  delete process.env.OPENCODEX_ACL_VERIFY_EXISTING;
  original = JSON.stringify(fixture());
  writeFileSync(join(root, "config.json"), original);
  writeFileSync(join(root, "input.json"), JSON.stringify({ ...fixture(), effortCap: "high" }));
  restores = [];
  advisorConfig = undefined;
  for (const method of ["log", "error", "warn"] as const) {
    const hook = spyOn(console, method).mockImplementation(() => {}); restores.push(() => hook.mockRestore());
  }
  resetHardenedStateForTests(); setPlatformForTests("win32");
  setSyntheticWindowsPrincipalForTests("*S-1-5-21-1-2-3-1001"); setIcaclsRunnerForTests(() => success);
});
afterEach(async () => {
  await paths.flushConfigDirHardeningAndReaps(root);
  for (const restore of restores.reverse()) restore();
  setAsyncIcaclsRunnerForTests(null); setAsyncIcaclsBeltSchedulerForTests(null);
  setNowForTests(null); setIcaclsRunnerForTests(null); setPlatformForTests(null);
  setSyntheticWindowsPrincipalForTests(null); resetHardenedStateForTests();
  keys.forEach((key, index) => {
    if (previous[index] === undefined) delete process.env[key]; else process.env[key] = previous[index];
  });
  removeTreeWithRetry(root);
});
function clock() {
  let now = 0;
  const timers = new Map<() => void, number>();
  setNowForTests(() => now);
  setAsyncIcaclsBeltSchedulerForTests((callback, milliseconds) => {
    timers.set(callback, now + milliseconds); return () => { timers.delete(callback); };
  });
  return { advance(milliseconds: number) {
    now += milliseconds;
    for (const [callback, deadline] of timers) if (deadline <= now) { timers.delete(callback); callback(); }
  } };
}
function holdParent() {
  const entered = deferred(), released = deferred(), reached = deferred();
  let held = true;
  const descendants: string[] = [];
  setAsyncIcaclsRunnerForTests(async () => { entered.resolve(); await released.promise; return success; });
  setIcaclsRunnerForTests(args => {
    if (held && args[0]?.startsWith(root + sep)) { descendants.push(args[0]); reached.resolve(); }
    return success;
  });
  const barrier = paths.withConfigDirHardeningSettled;
  const hook = spyOn(paths, "withConfigDirHardeningSettled").mockImplementation(<T>(dir: string, mutate: () => T) => {
    const result = barrier(dir, mutate); reached.resolve(); return result;
  });
  restores.push(() => hook.mockRestore()); paths.hardenConfigDir();
  return { entered: entered.promise, reached: reached.promise, arrive: reached.resolve, descendants,
    release() { held = false; released.resolve(); } };
}
const operations = ["set", "unset", "import", "effort", "model-add", "model-remove", "provider-add", "provider-remove", "provider-default"] as const;
function run(operation: typeof operations[number]) {
  if (operation === "effort") return handleEffortCommand(["high", "--json"], { findLiveProxy: async () => null });
  if (operation.startsWith("model-") || operation.startsWith("provider-")) {
    const work = operation === "model-add" ? handleModels(["add", "mock", "custom-after", "--json"], { findLiveProxy: async () => null })
      : operation === "model-remove" ? handleModels(["remove", "fixture-model", "--yes", "--json"], { findLiveProxy: async () => null })
      : handleProviderCommand(operation === "provider-add" ? ["add", "third", "--adapter", "openai-chat", "--base-url", "https://third.example/v1", "--json"]
        : operation === "provider-remove" ? ["remove", "second", "--json"] : ["set-default", "second", "--json"]);
    return work.then(() => 0, error => { if (error.code !== "ETIMEDOUT") throw error; expect(error.code).toBe("ETIMEDOUT"); return 1; });
  }
  return handleConfigCommand(operation === "import" ? ["import", join(root, "input.json"), "--yes", "--json"]
    : operation === "set" ? ["set", "effortCap", "high", "--json"] : ["unset", "effortCap", "--json"]);
}
for (const operation of operations) {
  test.each([false, true])(`${operation} waits for parent ACL admission (timeout=%s)`, async timeout => {
    const time = clock(), parent = holdParent(); await parent.entered;
    let settled = false;
    const work = run(operation).then(value => { settled = true; return value; });
    void work.finally(parent.arrive);
    try {
      await parent.reached; await turn();
      expect(parent.descendants).toEqual([]); expect(settled).toBe(false);
      if (operation === "advisor") expect(advisorConfig?.advisor).toBeUndefined();
      expect(readFileSync(join(root, "config.json"), "utf8")).toBe(original);
      expect(existsSync(join(root, "config-mutation.sqlite"))).toBe(false);
      if (timeout) {
        time.advance(999); await turn(); expect(settled).toBe(false); time.advance(1);
        expect(await work).toBe(1);
        expect(readFileSync(join(root, "config.json"), "utf8")).toBe(original);
        expect(parent.descendants).toEqual([]);
        if (operation === "advisor") expect(advisorConfig?.advisor).toBeUndefined();
        expect(existsSync(join(root, "config-mutation.sqlite"))).toBe(false);
        time.advance(100_000); await paths.flushConfigDirHardening(root);
        expect(windowsSecretAclReapPendingForPath(root)).toBe(true);
      } else if (operation === "import") {
        writeFileSync(join(root, "input.json"), JSON.stringify({ ...fixture(), effortCap: "high", subagentEffortCap: "medium" }));
      } else {
        writeFileSync(join(root, "config.json"), JSON.stringify({ ...fixture(), subagentEffortCap: "medium" }));
      }
    } finally { parent.release(); await work; }
    if (!timeout) {
      expect(await work).toBe(0);
      const saved = JSON.parse(readFileSync(join(root, "config.json"), "utf8"));
      if (["set", "unset", "import", "effort"].includes(operation)) expect(saved.effortCap).toBe(operation === "unset" ? undefined : "high");
      if (operation === "model-add") expect(saved.customModels.map((row: { modelId: string }) => row.modelId)).toEqual(["custom-before", "custom-after"]);
      if (operation === "model-remove") expect(saved.customModels).toBeUndefined();
      if (operation === "provider-add") expect(saved.providers.third.baseUrl).toBe("https://third.example/v1");
      if (operation === "provider-remove") expect(saved.providers.second).toBeUndefined();
      if (operation === "provider-default") expect(saved.defaultProvider).toBe("second");
      if (operation === "advisor") expect(saved.advisor.enabled).toBe(true);
      expect(saved.subagentEffortCap).toBe("medium");
    }
  });
}

test.each(["changed", "removed"])("custom-model removal refuses a %s confirmed row after admission", async change => {
  const exit = new Error("fixture CLI exit");
  const hook = spyOn(process, "exit").mockImplementation(() => { throw exit; });
  restores.push(() => hook.mockRestore());
  const parent = holdParent(); await parent.entered;
  const work = handleModels(["remove", "fixture-model", "--yes", "--json"], { findLiveProxy: async () => null })
    .then(() => undefined, error => error);
  try {
    await parent.reached; await turn();
    expect(parent.descendants).toEqual([]);
    const fresh = fixture();
    if (change === "changed") fresh.customModels[0]!.modelId = "concurrent-edit";
    else fresh.customModels = [];
    const bytes = JSON.stringify(fresh);
    writeFileSync(join(root, "config.json"), bytes);
    parent.release();
    expect(await work).toBe(exit);
    expect(readFileSync(join(root, "config.json"), "utf8")).toBe(bytes);
    expect(existsSync(join(root, "config-mutation.sqlite"))).toBe(false);
  } finally { parent.release(); await work; }
});

const cliOperations = operations.filter(operation => operation !== "advisor");
for (const operation of cliOperations) {
  test.each([false, true])(`${operation} keeps retired-provider recovery inside admission (timeout=%s)`, async timeout => {
    original = JSON.stringify({ ...fixture(), providers: { ...fixture().providers,
      "opencode-free": { adapter: "openai-chat", baseUrl: "https://retired.example/v1", apiKey: "synthetic-recovery-secret" },
    } });
    writeFileSync(join(root, "config.json"), original);
    const time = clock(), parent = holdParent(); await parent.entered;
    const work = run(operation);
    void work.then(parent.arrive, parent.arrive);
    try {
      await parent.reached; await turn();
      expect(parent.descendants).toEqual([]);
      expect(readdirSync(root).filter(name => name.includes("pre-opencode-free-removal"))).toEqual([]);
      expect(readFileSync(join(root, "config.json"), "utf8")).toBe(original);
      if (timeout) {
        time.advance(1000);
        expect(await work).toBe(1);
        expect(readFileSync(join(root, "config.json"), "utf8")).toBe(original);
        expect(parent.descendants).toEqual([]);
        expect(readdirSync(root).filter(name => name.includes("pre-opencode-free-removal"))).toEqual([]);
      }
    } finally { parent.release(); await work; }
    if (!timeout) {
      expect(await work).toBe(0);
      const backups = readdirSync(root).filter(name => name.includes("pre-opencode-free-removal") && name.endsWith(".bak"));
      expect(backups.length).toBeGreaterThan(0);
      expect(backups.some(name => readFileSync(join(root, name), "utf8") === original)).toBe(true);
    }
  });
}

test("GCP cleanup completes before a parent flight queued after provider publication", async () => {
  const config = fixture();
  Object.assign(config.providers.mock, { apiKey: "gcp-sa:writer-fixture" });
  writeFileSync(join(root, "config.json"), JSON.stringify(config));
  let deletions = 0;
  setProviderKeychainEntryFactoryForTests(() => ({ getPassword: () => null, setPassword: () => {}, deletePassword: () => { deletions++; return true; } }));
  restores.push(() => setProviderKeychainEntryFactoryForTests(null));
  const released = deferred(), entered = deferred();
  let held = false;
  const descendants: string[] = [];
  setAsyncIcaclsRunnerForTests(async () => { entered.resolve(); await released.promise; return success; });
  setIcaclsRunnerForTests(args => {
    if (held && args[0]?.startsWith(root + sep)) descendants.push(args[0]);
    return success;
  });
  const barrier = paths.withConfigDirHardeningSettled;
  const hook = spyOn(paths, "withConfigDirHardeningSettled").mockImplementation(<T>(dir: string, mutate: () => T) => barrier(dir, () => {
    const result = mutate();
    queueMicrotask(() => { held = true; paths.hardenConfigDir(); });
    return result;
  }));
  restores.push(() => hook.mockRestore());
  const work = handleProviderCommand(["add", "mock", "--force", "--adapter", "openai-chat", "--base-url", "https://mock.example/v1", "--json"]);
  try {
    await entered.promise; await turn();
    expect(descendants).toEqual([]);
    expect(deletions).toBe(1);
    expect(existsSync(join(root, "local-credentials.json"))).toBe(true);
  } finally { held = false; released.resolve(); await work; }
});

for (const operation of cliOperations) {
  test.each([false, true])(`${operation} refuses malformed authority without overwriting bytes (late=%s)`, async late => {
    const invalid = '{ "providers": { "operator-data": "preserve-me"';
    const exit = spyOn(process, "exit").mockImplementation(() => { throw new Error("unexpected CLI exit"); });
    restores.push(() => exit.mockRestore());
    const parent = late ? holdParent() : undefined;
    if (parent) await parent.entered;
    else writeFileSync(join(root, "config.json"), invalid);
    const work = run(operation).then(value => value, error => error);
    if (parent) {
      await parent.reached; await turn();
      expect(parent.descendants).toEqual([]);
      writeFileSync(join(root, "config.json"), invalid);
      parent.release();
    }
    try {
      const outcome = await work;
      if (["set", "unset", "import", "effort"].includes(operation)) expect(outcome).toBe(2);
      else expect(outcome).toBeInstanceOf(CliUsageError);
      expect(readFileSync(join(root, "config.json"), "utf8")).toBe(invalid);
      expect(readdirSync(root).filter(name => name.startsWith("config.json.invalid-"))).toEqual([]);
      expect(existsSync(join(root, "config-mutation.sqlite"))).toBe(false);
      expect(exit).not.toHaveBeenCalled();
    } finally { parent?.release(); await work; }
  });
}

const nonObjectRoots = ["[]", '[{"operator-original":"synthetic-data-that-must-survive"}]', "null", "true", "42", '"operator-original"'];
test.each(nonObjectRoots)("mutation reader refuses non-object authority %s", raw => {
  writeFileSync(join(root, "config.json"), raw);
  expect(() => readCliConfigForMutation({ passive: true })).toThrow(CliUsageError);
  expect(() => readCliConfigForMutation()).toThrow(CliUsageError);
  expect(readFileSync(join(root, "config.json"), "utf8")).toBe(raw);
  expect(readdirSync(root).filter(name => name.startsWith("config.json.invalid-"))).toEqual([]);
});
for (const operation of cliOperations) {
  test.each(nonObjectRoots)(`${operation} refuses non-object authority %s without changing bytes`, async raw => {
    writeFileSync(join(root, "config.json"), raw);
    const exit = spyOn(process, "exit").mockImplementation(() => { throw new Error("unexpected CLI exit"); });
    restores.push(() => exit.mockRestore());
    const work = operation === "model-add"
      ? handleModels(["add", "openai", "review-array-model", "--json"], { findLiveProxy: async () => null })
      : run(operation);
    const outcome = await work.then(value => value, error => error);
    expect(readFileSync(join(root, "config.json"), "utf8")).toBe(raw);
    expect(readdirSync(root).filter(name => name.startsWith("config.json.invalid-"))).toEqual([]);
    if (["set", "unset", "import", "effort"].includes(operation)) expect(outcome).toBe(2);
    else expect(outcome).toBeInstanceOf(CliUsageError);
    expect(existsSync(join(root, "config-mutation.sqlite"))).toBe(false);
    expect(exit).not.toHaveBeenCalled();
  });
}

test.each(["models", "provider"])("%s dispatch reports invalid authority with standard exit two", async command => {
  const raw = '{"providers":';
  writeFileSync(join(root, "config.json"), raw);
  const args = command === "models" ? [command, "add", "mock", "new-model", "--json"]
    : [command, "add", "third", "--adapter", "openai-chat", "--base-url", "https://third.example/v1", "--json"];
  const error = spyOn(console, "error").mockImplementation(() => {});
  restores.push(() => error.mockRestore());
  const previousExit = process.exitCode;
  try {
    process.exitCode = 79;
    expect(await dispatchCommand({ kind: "command", command, args }, { args } as CliDispatchDeps)).toBe(2);
    expect(error.mock.calls).toEqual([["Error: config.json is invalid or unreadable; run ocx config validate and repair it before retrying. Nothing was saved."]]);
    expect(readFileSync(join(root, "config.json"), "utf8")).toBe(raw);
    expect(existsSync(join(root, "config-mutation.sqlite"))).toBe(false);
  } finally { process.exitCode = previousExit; }
});
