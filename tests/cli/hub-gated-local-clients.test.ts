import { afterEach, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import type { ExportModel } from "../../backend/src/clients/config-export";
import { INTEGRATION_CLIENTS } from "../../backend/src/integrations/registry";
import { createIntegrationStateStore } from "../../backend/src/integrations/store";
import { applyIntegration } from "../../backend/src/integrations/writer";
import { codexInjectLockOutcome } from "../../backend/src/codex/inject-coordination";
import {
  localClientSkipMessage,
  localClientSkipReason,
  localClientSyncAllowed,
  shouldSyncCodexOnStart,
  shouldSyncGrokOnStart,
  syncCodexOnStartIfEnabled,
} from "../../backend/src/codex/desired-state";
import { markSiblingStart, resetSiblingStartForTests, siblingSkipMessage } from "../../backend/src/codex/sibling-start";
import { createManagementConvergeCodex } from "../../backend/src/codex/management-convergence";
import { createCatalogConvergeRequest } from "../../backend/src/codex/catalog-admission";
import { injectClaudeAgentDefs } from "../../backend/src/claude/agents-inject";
import { refreshOwnedCatalogIntegrations } from "../../backend/src/integrations/catalog-refresh";
import { syncEnabledClientIntegrations } from "../../backend/src/server/management/config-routes";
import { createReadinessGate } from "../../backend/src/server/readiness";
import type { OcxConfig } from "../../backend/src/types";
import { removeTreeWithRetry } from "../helpers/remove-tree";

describe("a sibling instance never writes the live owner's client routing", () => {
  afterEach(() => resetSiblingStartForTests());
  const plain = (overrides: Partial<OcxConfig> = {}): OcxConfig => ({
    port: 10_100,
    providers: {},
    defaultProvider: "openai",
    ...overrides,
  }) as unknown as OcxConfig;

  test("the central gate is closed with its own reason and sentence, and only while marked", () => {
    expect(localClientSyncAllowed(plain())).toBe(true);
    expect(shouldSyncCodexOnStart(plain())).toBe(true);
    markSiblingStart(10_100);
    expect(localClientSyncAllowed(plain())).toBe(false);
    expect(localClientSyncAllowed(plain({ unauthenticatedLoopbackListener: { enabled: true } }))).toBe(false);
    expect(shouldSyncCodexOnStart(plain())).toBe(false);
    expect(shouldSyncGrokOnStart(plain())).toBe(false);
    // The sibling mark is decisive: it is the reason nothing was written.
    expect(localClientSkipReason()).toBe("sibling");
    expect(localClientSkipMessage("Codex integration is OFF")).toBe(siblingSkipMessage());
    expect(siblingSkipMessage()).toContain("port 10100");
    expect(siblingSkipMessage(10_199)).toBe(
      "Client routing stays on the proxy at port 10100; this instance serves direct requests on port 10199 only.",
    );
    const lock = codexInjectLockOutcome({ status: "skipped", reason: "sibling", waitedMs: 0 });
    expect(lock).toMatchObject({ success: true, status: "skipped", skippedReason: "sibling" });
    expect(lock.message).toContain(siblingSkipMessage());
    expect(lock.message).not.toContain("integration is OFF");

    resetSiblingStartForTests();
    expect(localClientSyncAllowed(plain())).toBe(true);
  });

  test("startup sync never runs and readiness still settles", async () => {
    markSiblingStart(10_100);
    const gate = createReadinessGate();
    let syncCalls = 0;
    const result = await syncCodexOnStartIfEnabled(10_199, plain(), async () => {
      syncCalls += 1;
      return { ok: true, catalogWritten: true, cacheSynced: true };
    }, gate);
    expect(result).toEqual({ ran: false, catalogWritten: false, cacheSynced: false });
    expect(syncCalls).toBe(0);
    // /readyz must not hang pending for an instance that deliberately wrote nothing.
    expect(gate.getStatus()).toBe("ready");
  });

  test("the Codex sync every caller runs reports the sibling, not the toggle", async () => {
    markSiblingStart(10_100);
    const { syncModelsToCodex } = await import("../../backend/src/codex/sync");
    const result = await syncModelsToCodex(10_199, plain(), null);
    expect(result).toMatchObject({ status: "skipped", skippedReason: "sibling", ok: true, catalogWritten: false, cacheSynced: false });
    expect(result.message).toBe(siblingSkipMessage());
  });

  test("the Claude roster and the catalog funnel both refuse", async () => {
    markSiblingStart(10_100);

    const agentsDir = mkdtempSync(join(tmpdir(), "ocx-sibling-agents-"));
    try {
      expect(injectClaudeAgentDefs(plain(), {}, agentsDir)).toBeNull();
      expect(readdirSync(agentsDir)).toEqual([]);
    } finally {
      removeTreeWithRetry(agentsDir);
    }

    const converge = createManagementConvergeCodex(plain());
    const outcome = await converge(createCatalogConvergeRequest({ deadlineMs: 1_000 }));
    expect(outcome).toMatchObject({
      kind: "catalog-only",
      changed: false,
      catalogRefresh: { status: "skipped", reason: "refused", retryable: false },
    });
  });

  test("an owned client file keeps the owner's port while marked, and is refreshed once unmarked", async () => {
    const root = mkdtempSync(join(tmpdir(), "ocx-sibling-owned-"));
    try {
      const env: NodeJS.ProcessEnv = {};
      const home = join(root, "home");
      const store = createIntegrationStateStore(join(root, "state", "integrations"));
      const pi = INTEGRATION_CLIENTS.pi;
      mkdirSync(pi.detectDir(env, home), { recursive: true });
      mkdirSync(dirname(pi.configPath(env, home)), { recursive: true });
      writeFileSync(pi.configPath(env, home), JSON.stringify({ providers: {} }));
      const config = plain({
        hostname: "127.0.0.1",
        defaultProvider: "mock",
        providers: { mock: { adapter: "openai-chat", baseUrl: "http://127.0.0.1/v1" } },
      } as Partial<OcxConfig>);
      const models: ExportModel[] = [{ namespaced: "mock/a", provider: "mock", id: "a", contextWindow: 128_000 }];
      // Owned: connected by the live owner, at the owner's port.
      expect(applyIntegration({ clientId: "pi", models, config, port: 10_100, env, home, store }).ok).toBe(true);
      const owned = readFileSync(pi.configPath(env, home), "utf8");
      expect(owned).toContain("http://127.0.0.1:10100/v1");

      let loads = 0;
      const input = { models: async () => { loads += 1; return models; }, config, port: 10_199, env, home, store };
      markSiblingStart(10_100);
      expect(await refreshOwnedCatalogIntegrations(input, ["pi"])).toEqual([]);
      expect(loads).toBe(0);
      expect(readFileSync(pi.configPath(env, home), "utf8")).toBe(owned);

      // Unmarked control: the same call does re-point the owned file, so the [] above is the mark's.
      resetSiblingStartForTests();
      expect(await refreshOwnedCatalogIntegrations(input, ["pi"])).toEqual([{ client: "pi", ok: true, changed: true }]);
      expect(loads).toBe(1);
      expect(readFileSync(pi.configPath(env, home), "utf8")).toContain("http://127.0.0.1:10199/v1");
    } finally {
      removeTreeWithRetry(root);
    }
  });

  test("PUT /api/settings and /api/sync fan-out re-points no Grok fence or Desktop profile at this port", async () => {
    const calls: string[] = [];
    const deps = {
      fetchAllModels: async () => { calls.push("fetchAllModels"); return []; },
      refreshOwnedCatalogIntegrations: async () => { calls.push("refreshOwned"); return []; },
      writeDesktop3pConfig: () => { calls.push("writeDesktop3pConfig"); return { written: false, reason: "test" }; },
    } as unknown as Parameters<typeof syncEnabledClientIntegrations>[2];

    markSiblingStart(10_100);
    // Every client ON: without the mark this would reach syncGrokConfig and the Desktop writer.
    expect(await syncEnabledClientIntegrations(10_199, plain(), deps)).toEqual([]);
    expect(calls).toEqual([]);

    // Unmarked, the same call does fan out; the mark is what stopped it.
    resetSiblingStartForTests();
    const off = plain({ clientIntegrations: { grok: false, "claude-desktop": false } });
    await syncEnabledClientIntegrations(10_199, off, deps);
    expect(calls).toEqual(["refreshOwned"]);
  });
});
