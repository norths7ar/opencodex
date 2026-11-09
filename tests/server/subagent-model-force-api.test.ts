import { expect, mock, test } from "bun:test";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { handleSubagentModelRoutes } from "../../src/server/management/subagent-model-routes";
import type { ManagementContext } from "../../src/server/management/context";
import { loadConfig, setPersistedConfigMutationBeforeCommitForTests } from "../../src/config";
import type { OcxConfig } from "../../src/types";
import { InitialConfigPublicationError } from "../../src/config/initialize";
import { ConfigWritePublishedError } from "../../src/config/persist-unlocked";
import { removeTreeWithRetry } from "../helpers/remove-tree";

function cfg(): OcxConfig {
  return { port: 10100, providers: {}, defaultProvider: "openai", subagentModels: ["retired/model"],
    claudeCode: { subagentEffort: "high", model: "keep", subagentModelForce: "combo/tev-auto" } };
}
function context(config: OcxConfig, body: unknown): ManagementContext {
  const url = new URL("http://localhost/api/subagent-models");
  return { url, config, version: "test", req: new Request(url, { method: "PUT", body: JSON.stringify(body) }),
    deps: { fetchAllModels: async () => [{ provider: "combo", id: "tev-auto" }], saveConfigPreservingClaudeCode: mock(() => {}) },
    convergeCodexCatalog: mock(async () => ({ status: "committed", changed: true, degraded: false, notices: [] } as const)),
    syncClaudeAgentDefsBestEffort: mock(async () => {}) };
}
const apply = async () => {};

test("persistence failure restores both Claude and roster state", async () => {
  const config = cfg(); const before = structuredClone(config);
  const ctx = context(config, { force: null, models: [] });
  ctx.deps.saveConfigPreservingClaudeCode = () => { throw new Error("fixture save failure"); };
  await expect(handleSubagentModelRoutes(ctx, apply)).rejects.toThrow("fixture save failure");
  expect(config).toEqual(before);
});

test("field-scoped durable force writes preserve concurrent disk edits and clear only the force leaf", async () => {
  const home = mkdtempSync(join(tmpdir(), "ocx-force-api-"));
  const previous = process.env.OPENCODEX_HOME;
  process.env.OPENCODEX_HOME = home;
  try {
    const path = join(home, "config.json");
    writeFileSync(path, JSON.stringify(cfg()));
    const live = loadConfig();
    const disk = cfg(); disk.claudeCode!.model = "concurrent-model"; disk.claudeCode!.subagentEffort = "max";
    writeFileSync(path, JSON.stringify(disk));
    const ctx = context(live, { force: null }); delete ctx.deps.saveConfigPreservingClaudeCode;
    expect((await handleSubagentModelRoutes(ctx, apply))?.status).toBe(200);
    const saved = JSON.parse(readFileSync(path, "utf8"));
    expect(saved.claudeCode).toMatchObject({ model: "concurrent-model", subagentEffort: "max" });
    expect(saved.claudeCode.subagentModelForce).toBeUndefined();
  } finally {
    if (previous === undefined) delete process.env.OPENCODEX_HOME; else process.env.OPENCODEX_HOME = previous;
    removeTreeWithRetry(home);
  }
});


async function isolatedForceHome(run: (path: string) => Promise<void>) {
  const home = mkdtempSync(join(tmpdir(), "ocx-force-first-run-"));
  const previous = process.env.OPENCODEX_HOME;
  process.env.OPENCODEX_HOME = home;
  try { await run(join(home, "config.json")); }
  finally {
    setPersistedConfigMutationBeforeCommitForTests(null);
    if (previous === undefined) delete process.env.OPENCODEX_HOME; else process.env.OPENCODEX_HOME = previous;
    removeTreeWithRetry(home);
  }
}

for (const force of ["combo/tev-auto", null]) {
  test(`first-run force ${force === null ? "clear" : "set"} initializes absent config without losing live defaults`, () => isolatedForceHome(async path => {
    const live = loadConfig();
    live.claudeCode = { model: "keep-first-run", subagentEffort: "high" };
    const roster = [...(live.subagentModels ?? [])];
    const ctx = context(live, { force }); delete ctx.deps.saveConfigPreservingClaudeCode;
    const response = await handleSubagentModelRoutes(ctx, apply);
    expect(response?.status).toBe(200);
    const saved = JSON.parse(readFileSync(path, "utf8"));
    expect(saved.claudeCode).toMatchObject({ model: "keep-first-run", subagentEffort: "high" });
    expect(saved.claudeCode.subagentModelForce).toBe(force ?? undefined);
    expect(saved.subagentModels ?? []).toEqual(roster);
    expect(ctx.convergeCodexCatalog).not.toHaveBeenCalled();
  }));
}

for (const kind of ["corrupt", "directory", "dangling-link", "appeared", "deleted", "mutation-race"] as const) {
}


test.skipIf(process.platform === "win32")("unreadable config is preserved and force mutation fails closed", () => isolatedForceHome(async path => {
  const original = JSON.stringify(cfg()); writeFileSync(path, original); chmodSync(path, 0);
  try {
    const live = cfg(); const before = structuredClone(live);
    const ctx = context(live, { force: null }); delete ctx.deps.saveConfigPreservingClaudeCode;
    expect((await handleSubagentModelRoutes(ctx, apply))?.status).toBe(409);
    expect(live).toEqual(before);
  } finally { chmodSync(path, 0o600); }
  expect(readFileSync(path, "utf8")).toBe(original);
}));

for (const publication of ["not-published", "published", "uncertain"] as const) {
  test(`initial publication ${publication} error honors the durable rollback boundary`, async () => {
    const live = cfg(); const before = structuredClone(live);
    const ctx = context(live, { force: null });
    ctx.deps.saveConfigPreservingClaudeCode = () => { throw new InitialConfigPublicationError(publication, false, false); };
    await expect(handleSubagentModelRoutes(ctx, apply)).rejects.toBeInstanceOf(InitialConfigPublicationError);
    if (publication === "not-published") expect(live).toEqual(before);
    else expect(live.claudeCode?.subagentModelForce).toBeUndefined();
  });
}

test("existing-file published write error does not restore only live force state", async () => {
  const live = cfg(); const ctx = context(live, { force: null });
  ctx.deps.saveConfigPreservingClaudeCode = () => { throw new ConfigWritePublishedError(new Error("fixture post-publication")); };
  await expect(handleSubagentModelRoutes(ctx, apply)).rejects.toBeInstanceOf(ConfigWritePublishedError);
  expect(live.claudeCode?.subagentModelForce).toBeUndefined();
});
