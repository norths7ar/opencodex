import { spyOn } from "bun:test";
import { existsSync, mkdirSync } from "node:fs";
import { flushNativeMainStartupReleases } from "../../backend/src/codex/native-profile-startup";
import * as codexRuntime from "../../backend/src/codex/runtime";
import { deriveStartupHealth } from "../../backend/src/codex/autostart-health";
import { clearAccountNeedsReauth, clearAccountQuota } from "../../backend/src/codex/auth-api";
import { clearCodexUpstreamHealth, clearThreadAccountMap } from "../../backend/src/codex/routing";
import { resetCodexModelEntitlementCacheForTests } from "../../backend/src/codex/model-entitlements";
import { saveConfig } from "../../backend/src/config";
import { flushConfigDirHardeningForTests } from "../../backend/src/config/paths";
import { migrateSubagentModels } from "../../backend/src/config/subagent-models";
import { configuredAdminToken } from "../../backend/src/lib/admin-secrets";
import { resetDebugLogBufferForTests } from "../../backend/src/lib/debug-log-buffer";
import { resetDebugSettingsForTests } from "../../backend/src/lib/debug-settings";
import { flushWindowsSecretAclReapsBeforeRemoval } from "../../backend/src/lib/windows-secret-acl";
import { projectOpenAiTierMigration } from "../../backend/src/providers/openai-tiers";
import { clearHealthHistoryCacheForTests } from "../../backend/src/routing/health";
import { closeRequestHistoryIndex } from "../../backend/src/routing/history/indexer";
import { startServer, waitForFailedStartRollback } from "../../backend/src/server";
import { stopServerListener } from "../../backend/src/server/lifecycle";
import type { OcxConfig } from "../../backend/src/types";
import { ownedServiceHomeInspection } from "./owned-service-home-inspection";
import { removeTreeWithRetry } from "./remove-tree";

/** Seed a current installation without replaying unrelated upgrade writes at startup. */
export function currentServerFixtureConfig(config: OcxConfig): OcxConfig {
  // Use the production projections, not hand-maintained version flags. The caller
  // still publishes once through saveConfig with real file/directory ACL hardening.
  const current = projectOpenAiTierMigration(config).config;
  migrateSubagentModels(current);
  return current;
}

export function managementHeaders(initial?: HeadersInit): Headers {
  const token = configuredAdminToken();
  if (!token) throw new Error("management token was not initialized");
  const headers = new Headers(initial);
  headers.set("x-opencodex-api-key", token);
  return headers;
}

/** A runner timeout does not cancel its async body or execute its local finally first. */
function ownManagementServer(server: ReturnType<typeof startServer>, restoreRuntime: () => void) {
  const abort = new AbortController();
  let body: Promise<void> | undefined;
  let closing: Promise<void> | undefined;
  return {
    server,
    signal: abort.signal,
    run(work: () => Promise<void>): Promise<void> {
      abort.signal.throwIfAborted();
      body = work().catch(error => {
        if (closing && abort.signal.aborted && error instanceof Error && error.name === "AbortError") return;
        throw error;
      });
      return body;
    },
    close(): Promise<void> {
      return closing ??= (async () => {
        try {
          abort.abort();
          // Start the actual listener/lifecycle stop while the canceled request settles.
          // The existing stop helper memoizes this promise for repeated teardown callers.
          const stopped = Promise.allSettled([stopServerListener(server)]);
          await Promise.allSettled(body ? [body] : []);
          const [result] = await stopped;
          if (result.status === "rejected") throw result.reason;
        } finally {
          restoreRuntime();
        }
      })();
    },
  };
}

export type ManagementServerFixture = ReturnType<typeof ownManagementServer>;

/** Prepare real auth/ACL state while isolating unrelated host diagnostic projections. */
export async function startManagementServerFixture(
  configDir: string,
  fixtureConfig: OcxConfig,
): Promise<ManagementServerFixture> {
  if (existsSync(configDir)) removeTreeWithRetry(configDir);
  mkdirSync(configDir, { recursive: true });
  process.env.OPENCODEX_HOME = configDir;
  saveConfig({ ...fixtureConfig, clientIntegrations: { codex: false } });
  // Real config/token ACL preparation belongs to fixture readiness, not the HTTP
  // response deadline. Keep the production startup and the ordinary 5s test limit.
  // Neither management endpoint exercises native Codex synchronization or the
  // developer's installed service. Keep those external owners outside this fixture.
  const runtime = spyOn(codexRuntime, "getCodexRuntimeSnapshot").mockReturnValue({
    runtime: { command: "codex-fixture", version: null, source: "fallback" }, failures: [],
  });
  try {
    const server = startServer(0, {
      inspectNativeCodexOwnership: ownedServiceHomeInspection("management HTTP sandbox"),
      managementApi: {
        // /api/settings projects runtime/service diagnostics, but their host probes
        // are not auth/CORS behavior. Keep admission, routing and response decoration real.
        getCachedStartupHealth: async () => deriveStartupHealth({
          routingKind: "native", autostartEnabled: false, serviceInstalled: false,
          serviceViable: false, serviceEnabled: false, serviceRunning: false,
          serviceStale: false, serviceConflict: false, serviceSupported: true,
          shimInstalled: false, shimHealthy: false, platform: process.platform,
        }),
      },
    });
    return ownManagementServer(server, () => runtime.mockRestore());
  } catch (error) {
    try {
      await waitForFailedStartRollback(error);
    } finally {
      runtime.mockRestore();
    }
    throw error;
  }
}

/** Reset fixture evidence and settle producers while the caller still owns both homes. */
export async function settleServerAuthFixture(configDir: string, codexHome?: string): Promise<void> {
  clearCodexUpstreamHealth();
  clearThreadAccountMap();
  clearAccountNeedsReauth("pool-a");
  clearAccountNeedsReauth("pool-b");
  clearAccountQuota();
  resetCodexModelEntitlementCacheForTests();
  resetDebugSettingsForTests();
  resetDebugLogBufferForTests();
  // These producers and the process-wide SQLite index can outlive a stopped listener.
  // Drain/close them before the caller restores paths or removes the files.
  await flushNativeMainStartupReleases();
  await flushConfigDirHardeningForTests();
  clearHealthHistoryCacheForTests();
  closeRequestHistoryIndex();
  await flushWindowsSecretAclReapsBeforeRemoval(configDir);
  if (codexHome) await flushWindowsSecretAclReapsBeforeRemoval(codexHome);
}
