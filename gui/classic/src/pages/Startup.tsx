import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IconRefresh } from "../icons";
import { type TFn, useI18n } from "../i18n/shared";
import { readSessionListCache, writeSessionListCache } from "../session-list-cache";
import { Notice } from "../ui";
import { useDataSurface } from "../data-surface";
import { DataSurfaceSkeleton } from "../components/data-surface";
import { startupPresentation } from "../startup-health-ui";
import {
  StartupDetailsSection,
  StartupHeroSection,
  StartupRecoverySection,
} from "./startup-sections";
import {
  type StartupHealthData,
  type StartupInstallAction,
} from "./startup-shared";

type CodexRuntimeSettings = {
  version?: string | null;
  newerAvailable?: { path?: string; version?: string | null } | null;
  catalogClamp?: { active?: boolean; removedEfforts?: string[]; runtimeVersion?: string | null };
};

type StartupPageCache = {
  data: StartupHealthData;
  warning: string | null;
  fix: string | null;
};

const STARTUP_PAGE_CACHE_PREFIX = "ocx.startup.page.v2:";

function shellChain(commands: string[], platform: string | undefined): string {
  // Windows PowerShell 5.x rejects bash `&&`; `;` works in PowerShell and cmd.
  const sep = platform === "win32" ? "; " : " && ";
  return commands.join(sep);
}

function deriveCodexRuntimeNotice(
  runtime: CodexRuntimeSettings | undefined,
  t: TFn,
  platform?: string,
): { warning: string | null; fix: string | null } {
  if (!runtime) return { warning: null, fix: null };
  const clampActive = Boolean(runtime.catalogClamp?.active);
  const newer = Boolean(runtime.newerAvailable);
  const version = (clampActive
    ? runtime.catalogClamp?.runtimeVersion
    : runtime.version) ?? runtime.version ?? "unknown";
  const efforts = (runtime.catalogClamp?.removedEfforts ?? []).join(", ");
  const doctorSync = shellChain(["ocx doctor --fix-codex-runtime", "ocx sync"], platform);
  if (clampActive) {
    return {
      warning: efforts
        ? t("startup.codexRuntime.clampHiddenWithEfforts", { version, efforts })
        : t("startup.codexRuntime.clampHidden", { version }),
      fix: newer ? doctorSync : "ocx sync",
    };
  }
  if (newer) {
    return {
      warning: t("startup.codexRuntime.olderBinary", { version }),
      fix: doctorSync,
    };
  }
  return { warning: null, fix: null };
}

export default function Startup({ apiBase }: { apiBase: string }) {
  const { t } = useI18n();
  const cacheKey = `${STARTUP_PAGE_CACHE_PREFIX}${apiBase}`;
  const cached = useMemo(() => readSessionListCache<StartupPageCache>(cacheKey), [cacheKey]);
  const startupResourceKey = `startup-page:${apiBase}`;

  const [copied, setCopied] = useState<string | null>(null);
  const [installBusy, setInstallBusy] = useState<StartupInstallAction | null>(null);
  const [installResult, setInstallResult] = useState<{ kind: "success" | "error"; action: StartupInstallAction; repair?: boolean; detail?: string; forLocalRouting?: boolean } | null>(null);
  const [codexRuntimeWarning, setCodexRuntimeWarning] = useState<string | null>(() => cached?.warning ?? null);
  const [codexRuntimeFix, setCodexRuntimeFix] = useState<string | null>(() => cached?.fix ?? null);
  /** True while settings (runtime notice) are still in flight — reserves notice slot height. */
  const [runtimeNoticePending, setRuntimeNoticePending] = useState(() => !cached?.data);
  const paintedRef = useRef(Boolean(cached?.data));
  const secondaryGenerationRef = useRef(0);
  useEffect(() => () => {
    secondaryGenerationRef.current += 1;
  }, [apiBase]);

  const fetchStartup = useCallback(async (signal: AbortSignal): Promise<StartupHealthData> => {
    const secondaryGeneration = ++secondaryGenerationRef.current;
    const keepSecondary = paintedRef.current;
    // Keep prior notice visible on revalidation; only reserve empty slots on first paint.
    if (!keepSecondary) {
      setRuntimeNoticePending(true);
    }
    try {
      // Kick settings off immediately so it overlaps the health round-trip.
      const settingsPromise = fetch(`${apiBase}/api/settings`, { signal })
        .then(async (settingsRes) => {
          if (!settingsRes.ok) return null;
          return await settingsRes.json() as { codexRuntime?: CodexRuntimeSettings };
        })
        .catch(() => null);

      const res = await fetch(`${apiBase}/api/startup-health`, { signal });
      if (!res.ok) throw new Error("fetch failed");
      const next = await res.json() as StartupHealthData;
      // #1245: a failed install notice is a claim about ONE attempt, not about the
      // current state. Once health independently shows that attempt's goal is met —
      // the user may well have reached it another way — the notice contradicts what
      // the same page is showing and must yield.
      //
      // Scoped per action deliberately. `status` is overall restart safety, so a
      // machine protected by the service would otherwise erase a failed SHIM
      // install, which is still true and still actionable. Each action clears only
      // against the health field it was trying to change; restoring native routing
      // retires both, since neither install is outstanding then.
      setInstallResult(current => {
        if (current?.kind !== "error") return current;
        // Native routing retires an install that existed to protect a local
        // routing dependency. It does not retire an optional shim a native
        // machine can still install — that button is still on the page.
        if (next.status === "native" && current.forLocalRouting === true) return null;
        const satisfied = current.action === "install-service"
          // serviceViable, not installed-and-running: a stale or conflicting
          // service can be both while the page still reports it unhealthy.
          ? next.serviceViable
          // An activation-pending shim is installed; only the user's shell can finish it.
          : next.shimInstalled && (next.shimHealthy || next.shimActivationPending === true);
        return satisfied ? null : current;
      });
      paintedRef.current = true;
      const prevCache = readSessionListCache<StartupPageCache>(cacheKey);
      writeSessionListCache(cacheKey, {
        data: next,
        warning: prevCache?.warning ?? null,
        fix: prevCache?.fix ?? null,
      } satisfies StartupPageCache);

      // Health drives the main page, so publish it before the lower-priority settings request finishes.
      void settingsPromise.then(settings => {
        if (signal.aborted || secondaryGeneration !== secondaryGenerationRef.current) return;
        setRuntimeNoticePending(false);

        if (settings) {
          const notice = deriveCodexRuntimeNotice(settings.codexRuntime, t, next.platform);
          setCodexRuntimeWarning(notice.warning);
          setCodexRuntimeFix(notice.fix);
          writeSessionListCache(cacheKey, {
            data: next,
            warning: notice.warning,
            fix: notice.fix,
          } satisfies StartupPageCache);
          return;
        }

        // Settings fetch failure: keep the last-good runtime notice in UI + cache.
        const prev = readSessionListCache<StartupPageCache>(cacheKey);
        writeSessionListCache(cacheKey, {
          data: next,
          warning: prev?.warning ?? null,
          fix: prev?.fix ?? null,
        } satisfies StartupPageCache);
      });
      return next;
    } catch (error) {
      if (signal.aborted) throw error;
      if (!keepSecondary) {
        setCodexRuntimeWarning(null);
        setCodexRuntimeFix(null);
      }
      setRuntimeNoticePending(false);
      throw error;
    }
  }, [apiBase, cacheKey, t]);

  const startupResource = useDataSurface<StartupHealthData>(
    startupResourceKey,
    [apiBase],
    fetchStartup,
    { isEmpty: () => false, initialData: cached?.data ?? undefined },
  );
  const loadState = startupResource.state;
  const refresh = startupResource.refresh;
  const data = loadState.data ?? cached?.data ?? null;
  // Keep Refresh / install actions disabled for the whole in-flight window, including
  // warm revisits where `data` is already seeded from session cache.
  const loading = loadState.refreshing;
  const failed = Boolean(data?.diagnosticStale) || loadState.showError;
  const unverified = data !== null && startupPresentation(data, failed) === "unverified";

  useEffect(() => {
    if (!data?.diagnosticStale) return;
    const timer = window.setTimeout(refresh, 2000);
    return () => window.clearTimeout(timer);
  }, [data, refresh]);

  const copyCommand = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(command);
      window.setTimeout(() => setCopied(current => current === command ? null : current), 1600);
    } catch {
      setCopied(null);
    }
  };

  const runInstallAction = async (action: StartupInstallAction, opts?: { repair?: boolean }) => {
    setInstallBusy(action);
    setInstallResult(null);
    try {
      const res = await fetch(`${apiBase}/api/startup-action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, repair: opts?.repair === true }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null) as { error?: unknown } | null;
        throw new Error(typeof body?.error === "string" ? body.error : "installation failed");
      }
      setInstallResult({ kind: "success", action, repair: opts?.repair === true });
      refresh();
    } catch (error) {
      // #1245: remember whether this attempt was made while startup depended on
      // local routing. A later switch to native retires an install that existed to
      // protect that dependency, but says nothing about an optional shim a native
      // machine can still choose to install.
      setInstallResult({ kind: "error", action, repair: opts?.repair === true, detail: error instanceof Error ? error.message : String(error), forLocalRouting: data?.localRoutingDependency === true });
    } finally {
      setInstallBusy(null);
    }
  };

  return (
    <>
      <div className="page-head">
        <h2>{t("startup.title")}</h2>
        {/* The back button duplicated the sidebar; the explanatory sentence moved into the hero. */}
        <div className="startup-page-head-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => refresh()} disabled={loading}>
            <IconRefresh /> {t("startup.refresh")}
          </button>
        </div>
      </div>

      {loadState.showSkeleton && !data ? (
        <DataSurfaceSkeleton label={t("startup.loading")} rows={5} />
      ) : loadState.kind === "failed-cold" ? (
        <div className="startup-page-notice">
          <Notice tone="err">{loadState.error instanceof Error ? loadState.error.message : t("startup.error")}</Notice>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => refresh()}>{t("common.retry")}</button>
        </div>
      ) : data ? (
        <>
          {loadState.showError && !unverified && <Notice tone="err">{t("startup.error")}</Notice>}
          {failed && !unverified && (
            <div className="notice notice-warn startup-page-notice" role="alert">
              {t("startup.staleData")}
            </div>
          )}
          {(runtimeNoticePending || codexRuntimeWarning) && (
            <div
              className={`startup-runtime-notice-slot${runtimeNoticePending && !codexRuntimeWarning ? " startup-runtime-notice-slot--pending" : ""}`}
              aria-hidden={runtimeNoticePending && !codexRuntimeWarning ? true : undefined}
            >
              {codexRuntimeWarning && (
                <div className="notice notice-warn startup-page-notice startup-runtime-notice" role="status">
                  <p className="startup-runtime-notice__text">{codexRuntimeWarning}</p>
                  {codexRuntimeFix && (
                    <div className="startup-runtime-notice__fix">
                      <code>{codexRuntimeFix}</code>
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => void copyCommand(codexRuntimeFix)}>
                        {copied === codexRuntimeFix ? t("startup.copied") : t("startup.copy")}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          <StartupHeroSection failed={failed} data={data} />
          <StartupDetailsSection
            data={data}
            failed={failed}
            loading={loading}
            installBusy={installBusy}
            installResult={installResult}
            onInstall={(action, opts) => { void runInstallAction(action, opts); }}
          />
          <StartupRecoverySection data={data} copied={copied} onCopy={(command) => { void copyCommand(command); }} />
        </>
      ) : null}
    </>
  );
}
