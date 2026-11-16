import { Button } from "@/components/button";
import { clientApplicationWarning } from "@/contracts/application";
import type { ApplicationReceipt } from "@/contracts/channels";
import { useSubagentDelegation } from "@/features/subagents/use-native-settings";
import { useCallback, useEffect, useRef, useState } from "react";
import { DataSurfaceSkeleton } from "../components/data-surface";
import { PageHeader } from "../components/page-header";
import {
  subagentModelIdentity,
  subagentModelOptions,
  uniqueSubagentModels,
} from "../components/subagents-workspace/model-options";
import SubagentsWorkspace, {
  FEATURED_MAX,
} from "../components/subagents-workspace/SubagentsWorkspace";
import { InEffectSummary } from "../components/v2-subagents/InEffectSummary";
import WorkflowBudgetPanel from "../components/WorkflowBudgetPanel";
import { useDataSurface } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { useI18n } from "../i18n/shared";
import { formatNamespacedModelId } from "../provider-icons";
import { sectionAnchorId } from "../section-anchors";
import { readSessionListCache, writeSessionListCache } from "../session-list-cache";
import { Notice } from "../ui";

type CachedSubagents = {
  available: string[];
  chosen: string[];
  fallback?: string[];
  pollMs?: number;
  fallbackAvailable?: string[];
};

function seedSubagents(cacheKey: string): CachedSubagents | null {
  return readSessionListCache<CachedSubagents>(cacheKey);
}

/** Scrolls the page to one of its sections, for the section index. */
function jump(target: "featured" | "delegation" | "pin") {
  document
    .getElementById(sectionAnchorId("subagents", target))
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

export default function Subagents({ apiBase }: { apiBase: string }) {
  const { t, locale } = useI18n();
  const claudeAvailable = false;
  const cacheKey = `ocx.subagents.v1:${apiBase}`;
  const cached = seedSubagents(cacheKey);
  const [chosen, setChosen] = useState<string[]>(() => cached?.chosen ?? []);
  const [fallback, setFallback] = useState<string[]>(() => cached?.fallback ?? []);
  const [fallbackPollMs, setFallbackPollMs] = useState(() => cached?.pollMs ?? 60000);
  const [fallbackBusy, setFallbackBusy] = useState(false);
  const [fallbackLoaded, setFallbackLoaded] = useState(
    () => Array.isArray(cached?.fallback) && Number.isInteger(cached?.pollMs),
  );
  const [fallbackAvailable, setFallbackAvailable] = useState<string[] | undefined>(
    () => cached?.fallbackAvailable,
  );
  const [fallbackError, setFallbackError] = useState("");
  const [fallbackLoading, setFallbackLoading] = useState(true);
  const fallbackLoadController = useRef<AbortController | null>(null);
  const fallbackSnapshot = useRef<
    Pick<CachedSubagents, "fallback" | "pollMs" | "fallbackAvailable">
  >({
    fallback: cached?.fallback,
    pollMs: cached?.pollMs,
    fallbackAvailable: cached?.fallbackAvailable,
  });
  const fallbackRevision = useRef(0);
  const rosterRevision = useRef(0);
  const fallbackSaveInFlight = useRef(false);
  const committed = useRef<CachedSubagents | null>(cached);
  const [status, setStatus] = useState("");
  const [ok, setOk] = useState(false);
  useEffect(() => {
    if (!ok || !status) return;
    const timeout = window.setTimeout(() => setStatus(""), 4000);
    return () => window.clearTimeout(timeout);
  }, [ok, status]);
  /** True for the whole autosave drain, so a roster refresh never lands between two writes. */
  const saveInFlight = useRef(false);
  /** The newest roster waiting behind the in-flight write; older queued lists are dropped. */
  const queuedRoster = useRef<string[] | null>(null);
  /** Mirrors `chosen` synchronously, so two clicks in one render both build on the latest list. */
  const latestChosen = useRef<string[]>(chosen);
  const setRoster = useCallback((next: string[]) => {
    latestChosen.current = next;
    setChosen(next);
  }, []);
  const delegation = useSubagentDelegation(apiBase);
  /** The Claude Code pin as the pin panel last read or wrote it, for the summary tiles. */
  const pin = undefined;
  const currentApiBase = useRef(apiBase);
  const [clientApply, setClientApply] = useState<{ apiBase: string; pending: boolean } | null>(
    null,
  );
  useEffect(() => {
    currentApiBase.current = apiBase;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch(apiBase + "/api/codex-agent-settings", {
          signal: controller.signal,
        });
        const data = await readJsonOrThrow<{ pendingClientApply?: boolean }>(response);
        if (data && !controller.signal.aborted) {
          setClientApply({ apiBase, pending: data.pendingClientApply === true });
        }
      } catch {
        // The saved roster remains available when client apply status cannot be read.
      }
    })();
    return () => controller.abort();
  }, [apiBase]);
  const clientApplyCurrent = clientApply?.apiBase === apiBase;
  const pendingClientApply = clientApplyCurrent && clientApply.pending;

  const loadFallback = useCallback(async () => {
    fallbackLoadController.current?.abort();
    const controller = new AbortController();
    fallbackLoadController.current = controller;
    const { signal } = controller;
    const readRevision = fallbackRevision.current;
    try {
      const res = await fetch(`${apiBase}/api/subagent-model-fallback`, { signal });
      const data = await readJsonOrThrow<{
        models?: unknown;
        pollMs?: unknown;
        available?: unknown;
      }>(res);
      if (
        !data ||
        !Array.isArray(data.models) ||
        !data.models.every((model) => typeof model === "string" && model.trim()) ||
        typeof data.pollMs !== "number" ||
        !Number.isInteger(data.pollMs) ||
        data.pollMs < 5000 ||
        data.pollMs > 600000 ||
        !Array.isArray(data.available) ||
        !data.available.every((model) => typeof model === "string" && model.trim())
      ) {
        throw new Error(t("sub.loadFail"));
      }
      if (
        signal.aborted ||
        readRevision !== fallbackRevision.current ||
        fallbackSaveInFlight.current
      )
        return;
      const next = {
        fallback: data.models,
        pollMs: data.pollMs,
        fallbackAvailable: data.available,
      };
      fallbackSnapshot.current = next;
      setFallback(next.fallback);
      setFallbackPollMs(next.pollMs);
      setFallbackAvailable(next.fallbackAvailable);
      setFallbackLoaded(true);
      setFallbackError("");
      // An auxiliary success cannot seed a successful roster before its own read settles.
      if (committed.current) {
        committed.current = { ...committed.current, ...next };
        writeSessionListCache(cacheKey, committed.current);
      }
    } catch (error) {
      if (
        signal.aborted ||
        readRevision !== fallbackRevision.current ||
        fallbackSaveInFlight.current
      )
        return;
      setFallbackLoaded(false);
      setFallbackError(
        error instanceof Error && !(error instanceof SyntaxError)
          ? error.message
          : t("sub.loadFail"),
      );
    } finally {
      if (!signal.aborted) setFallbackLoading(false);
    }
  }, [apiBase, cacheKey, t]);

  useEffect(() => {
    void (async () => {
      await loadFallback();
    })();
    return () => {
      fallbackLoadController.current?.abort();
    };
  }, [loadFallback]);

  const loadSubagents = useCallback(
    async (signal?: AbortSignal): Promise<CachedSubagents> => {
      // Auxiliary fallback discovery must neither reject nor delay the roster resource.
      const rosterReadRevision = rosterRevision.current;
      const rosterRes = await fetch(`${apiBase}/api/subagent-models`, { signal });
      const response = await readJsonOrThrow<{ available?: string[]; chosen?: string[] }>(
        rosterRes,
        t("sub.loadFail"),
      );
      if (!response) throw new Error(t("sub.loadFail"));
      const available = response.available ?? [];
      const availableSet = new Set(available);
      const rosterCurrent = rosterReadRevision === rosterRevision.current && !saveInFlight.current;
      const next = {
        ...fallbackSnapshot.current,
        available,
        chosen: rosterCurrent
          ? (response.chosen ?? []).filter((model) => availableSet.has(model))
          : (committed.current?.chosen ?? []),
      };
      if (signal?.aborted) throw signal.reason;
      committed.current = next;
      if (rosterCurrent) setRoster(next.chosen);
      writeSessionListCache(cacheKey, next);
      return next;
    },
    [apiBase, cacheKey, setRoster, t],
  );

  // The shared resource owns mount loading and retries; the session seed keeps this workspace
  // usable while the first live response is in flight.
  const resource = useDataSurface<CachedSubagents>(cacheKey, [apiBase], loadSubagents, {
    isEmpty: () => false,
    initialData: cached ?? undefined,
  });
  const { state } = resource;
  const load = resource.refresh;
  const snapshot = state.data ?? cached;
  const available = snapshot?.available ?? [];
  const displayedChosen = uniqueSubagentModels(chosen);
  const displayedAvailable = subagentModelOptions(available, displayedChosen);

  // Every roster edit saves itself. Writes are serialized: while one PUT is in flight only the
  // newest edit waits, so rapid clicks end on the last list the operator made.
  const persistRoster = async (models: string[]) => {
    queuedRoster.current = models;
    if (saveInFlight.current) return;
    saveInFlight.current = true;
    setStatus("");
    let failure: unknown = null;
    let application: ApplicationReceipt | undefined;
    let applied = models;
    while (queuedRoster.current) {
      const sending = queuedRoster.current;
      queuedRoster.current = null;
      try {
        const r = await fetch(`${apiBase}/api/subagent-models`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ models: sending }),
        });
        const d = await readJsonOrThrow<{
          applied?: string[];
          catalogRefresh?: ApplicationReceipt;
        }>(r, t("sub.saveFailed"));
        application = d?.catalogRefresh;
        applied = d?.applied ?? sending;
        // The server holds this list even if a newer edit is queued, so it is the restore point.
        // A legacy roster-only seed does not prove that an empty fallback was loaded.
        committed.current = { ...committed.current, available, chosen: applied };
        writeSessionListCache(cacheKey, committed.current);
        failure = null;
      } catch (error) {
        failure = error;
      }
    }
    rosterRevision.current += 1;
    saveInFlight.current = false;
    if (failure) {
      setRoster(committed.current?.chosen ?? []);
      setOk(false);
      setStatus(
        failure instanceof Error && failure.message ? failure.message : t("sub.networkError"),
      );
      return;
    }
    setRoster(applied);
    const warning = clientApplicationWarning(application, t("integrations.catalogSyncFailed"));
    setOk(!warning);
    setStatus(warning ?? t("sub.saved", { n: applied.length, cmd: "ocx sync" }));
  };

  const editRoster = (next: string[]) => {
    rosterRevision.current += 1;
    setRoster(next);
    void persistRoster(next);
  };
  const toggle = (m: string) => {
    const prev = uniqueSubagentModels(latestChosen.current);
    const identity = subagentModelIdentity(m);
    const selected = prev.some((model) => subagentModelIdentity(model) === identity);
    if (!selected && prev.length >= FEATURED_MAX) return;
    editRoster(
      selected ? prev.filter((model) => subagentModelIdentity(model) !== identity) : [...prev, m],
    );
  };
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    const next = uniqueSubagentModels(latestChosen.current);
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j]!, next[i]!];
    editRoster(next);
  };

  const saveFallback = async () => {
    if (
      !fallbackLoaded ||
      fallbackSaveInFlight.current ||
      !Number.isInteger(fallbackPollMs) ||
      fallbackPollMs < 5000 ||
      fallbackPollMs > 600000
    )
      return;
    fallbackSaveInFlight.current = true;
    fallbackRevision.current += 1;
    const requestApiBase = apiBase;
    setFallbackBusy(true);
    setStatus("");
    try {
      const r = await fetch(`${apiBase}/api/subagent-model-fallback`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: fallback, pollMs: fallbackPollMs }),
      });
      const d = await readJsonOrThrow<{ models?: string[]; pollMs?: number }>(
        r,
        t("sub.fallbackSaveFailed"),
      );
      if (currentApiBase.current !== requestApiBase) return;
      if (!d || !Array.isArray(d.models) || typeof d.pollMs !== "number")
        throw new Error(t("sub.fallbackSaveFailed"));
      fallbackRevision.current += 1;
      setFallback(d.models);
      setFallbackPollMs(d.pollMs);
      fallbackSnapshot.current = {
        ...fallbackSnapshot.current,
        fallback: d.models,
        pollMs: d.pollMs,
      };
      const next = {
        available,
        chosen: committed.current?.chosen ?? [],
        ...fallbackSnapshot.current,
      };
      committed.current = next;
      writeSessionListCache(cacheKey, next);
      setOk(true);
      setStatus(t("sub.fallbackSaved"));
    } catch (error) {
      setOk(false);
      setStatus(error instanceof Error && error.message ? error.message : t("sub.networkError"));
    } finally {
      fallbackSaveInFlight.current = false;
      setFallbackBusy(false);
    }
  };

  const pageHeader = (
    <PageHeader
      title={t("nav.subagents")}
      subtitle={
        claudeAvailable
          ? t("subagentsV2.lede")
          : locale === "zh" || locale === "zh-TW"
            ? "管理 Codex 子代理的模型选择、委派方式和工作流预算。"
            : "Manage Codex subagent model selection, delegation and workflow budgets."
      }
    />
  );

  const cold = state.showSkeleton && !snapshot;
  useEffect(() => {
    if (cold) return;
    const followSection = () => {
      const section = new URLSearchParams(window.location.hash.split("?")[1]).get("section");
      if (section !== "settings" && section !== "budget") return;
      document
        .getElementById(sectionAnchorId("subagents", section))
        ?.scrollIntoView({ block: "start" });
    };
    const frame = requestAnimationFrame(followSection);
    window.addEventListener("hashchange", followSection);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("hashchange", followSection);
    };
  }, [cold]);

  if (state.kind === "failed-cold") {
    const reason = state.error instanceof Error ? state.error.message : t("sub.loadFail");
    return (
      <>
        {pageHeader}
        <Notice tone="err">{reason}</Notice>
        <Button type="button" variant="ghost" size="sm" onClick={() => load()}>
          {t("common.retry")}
        </Button>
      </>
    );
  }

  const firstOption = delegation.available.find((option) => option.namespaced === delegation.model);
  const firstModel = !delegation.model
    ? ""
    : firstOption
      ? formatNamespacedModelId(`${firstOption.provider}/${firstOption.model}`, t)
      : delegation.model;

  return (
    <>
      {pageHeader}
      {cold && <DataSurfaceSkeleton label={t("common.loading")} rows={5} />}
      <div
        aria-busy={cold || undefined}
        style={{ display: "contents", visibility: cold ? "hidden" : undefined }}
      >
        <div className="mb-6">
          <InEffectSummary
            chosen={displayedChosen}
            title={
              !clientApplyCurrent || pendingClientApply
                ? locale === "zh" || locale === "zh-TW"
                  ? pendingClientApply
                    ? "已保存 · Codex 未接入"
                    : "已保存的配置"
                  : pendingClientApply
                    ? "Saved · Codex not connected"
                    : "Saved configuration"
                : undefined
            }
            max={FEATURED_MAX}
            firstModel={firstModel}
            firstEffort={delegation.effort}
            pin={pin}
            showPin={claudeAvailable}
            onJump={jump}
          />
        </div>
        {delegation.error && (
          <Notice tone="err">
            {delegation.error}
            <Button type="button" variant="outline" size="sm" onClick={() => delegation.refresh()}>
              {t("common.retry")}
            </Button>
          </Notice>
        )}
        {status && <Notice tone={ok ? "ok" : "err"}>{status}</Notice>}
        {state.showError && <Notice tone="err">{t("sub.loadFail")}</Notice>}
        {fallbackError && !fallbackLoading && (
          <Notice tone="err">
            {t("sub.fallbackLabel")}: {t("sub.loadFail")}
            {fallbackError !== t("sub.loadFail") && <> {fallbackError}</>}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={fallbackLoading}
              onClick={() => {
                setFallbackLoading(true);
                void loadFallback();
              }}
            >
              {t("common.retry")}
            </Button>
          </Notice>
        )}
        <SubagentsWorkspace
          available={displayedAvailable}
          fallbackAvailable={fallbackAvailable ?? []}
          chosen={displayedChosen}
          onToggle={toggle}
          onMove={move}
          fallback={fallback}
          fallbackLoadState={
            fallbackAvailable !== undefined ? "ready" : fallbackLoading ? "loading" : "error"
          }
          fallbackPollMs={fallbackPollMs}
          fallbackBusy={fallbackBusy || !fallbackLoaded}
          onFallbackChange={(models) => {
            fallbackRevision.current += 1;
            setFallback(models);
          }}
          onFallbackPollMsChange={(pollMs) => {
            fallbackRevision.current += 1;
            setFallbackPollMs(pollMs);
          }}
          onFallbackSave={() => {
            void saveFallback();
          }}
          settingsExtra={
            <>
              <WorkflowBudgetPanel apiBase={apiBase} id={sectionAnchorId("subagents", "budget")} />
            </>
          }
          delegation={{
            apiBase,
            model: delegation.model,
            effort: delegation.effort,
            efforts: delegation.efforts,
            available: delegation.available,
            syncCodexDefaults: delegation.syncCodexDefaults,
            saving: delegation.saving,
            onSave: (patch) => {
              void delegation.save(patch);
            },
          }}
        />
      </div>
    </>
  );
}
