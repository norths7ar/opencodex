import type { CliDispatchDeps } from "./dispatch";
import type { OwnedIntegrationRefreshOutcome } from "../integrations/owned-refresh";
import { syncModelsToCodex } from "../codex/sync";
import { handleRestartScopeAfterWrite, readRestartScope } from "./restart-scope";
import { terminalSafeError } from "./runtime-api";
import { parseSyncFlags } from "./sync-flags";
import { projectLocalSyncResult } from "./local-sync-result";

export async function runSyncCommand(deps: CliDispatchDeps): Promise<number> {
    const flags = parseSyncFlags(deps.args.slice(1));
    if (!flags.ok) { console.error(flags.message); return 2; }
    const originalLog = console.log;
    if (flags.json) console.log = (...args: unknown[]) => console.error(...args);
    let result: ReturnType<typeof projectLocalSyncResult> | undefined;
    const runLocal = async (): Promise<number> => {
      const restartScope = readRestartScope(deps.args.slice(1), console);
      const live = await deps.findLiveProxy();
      const synced = await syncModelsToCodex(
        live?.port,
        undefined,
        undefined,
        undefined,
        { catalogEvenWhenNotInjected: true },
      );
      let code = 0;
      result = projectLocalSyncResult(synced);
      if (synced.status === "skipped") {
        console.log("Codex integration is OFF; sync skipped and no Codex files changed.");
      } else if (synced.status === "catalog-only") {
        // Explicit sync with the integration OFF still refreshes the catalog/cache
        // for side profiles that consume the proxy without injection.
        console.log(synced.message ?? "Codex integration is OFF; catalog refreshed, Codex config untouched.");
        if (!synced.ok) code = 1;
      } else if (!synced.ok) {
        code = 1;
        console.error("Codex sync did not complete. Fix the reported Codex config issue and retry.");
      }
      // Catalog/model-cache writes retain their warning/restart handling. This is
      // deliberately not an `else`: refreshCodexModelCatalog runs before injectCodexConfig,
      // so a sync can fail (`ok: false`) after the catalog was already rewritten — which is
      // exactly when a long-lived app-server is holding the stale list.
      // A configuration-only change also needs a reload, but only when explicitly
      // requested. A successful no-op or refused write is never restart authority.
      const requestedConfigRestart = (restartScope.appServers || restartScope.desktopApp)
        && synced.ok && synced.configChanged === true;
      if (synced.catalogWritten || synced.cacheSynced || requestedConfigRestart) {
        await handleRestartScopeAfterWrite(restartScope, console);
      }
      // `ocx sync` is a direct CLI path; it does not call the management
      // `/api/sync` route. Refresh already-connected file integrations here too,
      // after Codex has published the catalog that supplies its capabilities.
      if (synced.status !== "refused") {
        const results: OwnedIntegrationRefreshOutcome[] = [];
        if (live) {
          try {
            const config = deps.loadConfig();
            const { refreshOwnedCatalogIntegrations } = await import("../integrations/catalog-refresh");
            results.push(...await refreshOwnedCatalogIntegrations({
              models: async () => {
                const { loadExportModels } = await import("../server/management/model-rows");
                return loadExportModels(config);
              },
              config,
              port: live.port,
            }, ["mcode", "pi", "raycast", "omo", "cline", "commandcode", "droid", "opencode", "kilo", "qoder", "qoder-cn", "workbuddy", "workbuddy-ai", "hermes", "t3code"]));
          } catch (error) {
            console.warn(`Client integrations were not refreshed: ${error instanceof Error ? error.message : String(error)}`);
          }
        }
        // Even without a live proxy, report why Aside could not sync. Its server
        // owner is never bypassed, and another client's failure cannot hide it.
        try {
          const { refreshAsideProfilesThroughServer } = await import("./aside-profiles");
          results.push(...await refreshAsideProfilesThroughServer({ findLiveProxy: async () => live }));
        } catch (error) {
          console.warn(`Aside profiles were not refreshed: ${error instanceof Error ? error.message : String(error)}`);
        }
        for (const result of results) {
          const label = result.profileId === undefined ? result.client : `${result.client}:${result.profileId}`;
          if (result.changed) console.log(`${label} integration refreshed from the current catalog.`);
          else if (result.reason) console.warn(`${label} integration was not refreshed: ${result.reason}${result.residual ? " Recovery did not finish." : ""}${result.snapshotPath ? ` Backup: ${result.snapshotPath}` : ""}`);
        }
      }
      return code || (typeof process.exitCode === "number" ? process.exitCode : 0);
    };
    let code: number;
    try { code = await runLocal(); }
    catch (error) {
      if (!flags.json) throw error;
      console.error("Sync failed: " + terminalSafeError(error).message);
      code = 1;
    } finally { console.log = originalLog; }
    if (flags.json) originalLog(JSON.stringify({
      schema: "ocx.sync/1",
      ...(result ?? { status: "failed" }),
      ok: code === 0 && result?.ok === true,
      exit: code,
    }));
    return code;
}
