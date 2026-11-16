import { classifyDataSurface } from "../data-surface";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useKeyedClientResource } from "../client-resource";
import { useI18n } from "../i18n/shared";
import {
  clearSessionListCache,
  readSessionListCache,
  writeSessionListCache,
} from "../session-list-cache";
import {
  PROJECT_CONFIG_DIAGNOSTICS_POLL_MS,
  STARTUP_HEALTH_STALE_RETRY_MS,
  probeNeedsFastRetry,
  seedStartupHealthFromSettings,
  type StartupHealthStatus,
} from "../startup-health-ui";
import {
  fetchDashboardModels,
  fetchDashboardMultiAgent,
  fetchDashboardOverview,
  fetchDashboardSettings,
  fetchDashboardSidecars,
  fetchDashboardUsage,
  fetchProjectConfigDiagnostics,
  fetchStartupHealth,
  normalizeInjectionSelection,
  type DashboardEpochRefs,
} from "./dashboard-core-poll";
import { usageSummary30dResourceKey } from "../usage-summary-resource";
import {
  type DashboardSection,
  type HealthData,
  type ModelInfo,
  type ProjectCodexConfigGroup,
  type ProviderInfo,
  type SettingsData,
  type ShadowCallData,
  type SidecarData,
  type SidecarPatch,
  type SyncResult,
  type UsageSummary30d,
  type SidecarCodexApply,
  mergeSidecarSetting,
  sidecarPatchForSave,
  nextSidecarCodexApply,
  readDashboardSectionFromHash,
  requireJson,
  webSearchModelOptionsForPicker,
  visionModelOptions,
  useModalDialog,
} from "./dashboard-shared";
import { shadowCallSaveErrorKey } from "./models-shared";

const CONTROLS_CACHE_PREFIX = "ocx.dash.controls.v1:";
const OVERVIEW_CACHE_PREFIX = "ocx.dash.overview.v1:";
const USAGE_CACHE_PREFIX = "ocx.dash.usage30d.v1:";
const STARTUP_CACHE_PREFIX = "ocx.dash.startup.v4:";

type CachedControls = {
  settings?: SettingsData | null;
  sidecar?: SidecarData | null;
  shadowCall?: ShadowCallData | null;
};

type CachedOverview = {
  health: HealthData;
  providers: ProviderInfo[];
};

type CodexPreference = "codexAutoStart" | "codexDesktopAuthless" | "codexClientCompaction";
type DashboardSettingsState = {
  settings: SettingsData | null;
  beforeSave: SettingsData | null;
};
type DashboardSettingsAction =
  | { type: "polled"; settings: SettingsData }
  | { type: "save-started"; key: CodexPreference; value: boolean }
  | { type: "save-succeeded"; key: CodexPreference; settings: SettingsData }
  | { type: "save-failed" }
  | { type: "save-finished" }
  | { type: "applied" };

// Own both server snapshots and the local save/apply transaction. A poll has no
// application receipt and must not overwrite a preference while it is being saved.
function dashboardSettingsReducer(
  state: DashboardSettingsState,
  action: DashboardSettingsAction,
): DashboardSettingsState {
  switch (action.type) {
    case "polled":
      if (state.beforeSave) return state;
      return {
        ...state,
        settings: {
          ...action.settings,
          catalogRefreshPending:
            action.settings.catalogRefreshPending ?? state.settings?.catalogRefreshPending,
        },
      };
    case "save-started":
      if (!state.settings || state.beforeSave) return state;
      return {
        beforeSave: state.settings,
        settings: { ...state.settings, [action.key]: action.value },
      };
    case "save-succeeded":
      if (!state.settings || !state.beforeSave) return state;
      return {
        ...state,
        settings: {
          ...state.settings,
          ...action.settings,
          [action.key]: action.settings[action.key],
          catalogRefreshPending:
            action.settings.catalogRefreshPending ??
            (action.settings.catalogRefresh
              ? action.settings.catalogRefresh.status !== "applied"
              : action.key === "codexDesktopAuthless" || action.key === "codexClientCompaction"
                ? true
                : state.settings.catalogRefreshPending),
          catalogRefresh: action.settings.catalogRefresh,
          clientApplyWarning:
            action.settings.catalogRefresh?.warning ?? action.settings.clientApplyWarning,
          startupHealth: action.settings.startupHealth ?? state.settings.startupHealth,
        },
      };
    case "save-failed":
      return state.beforeSave ? { ...state, settings: state.beforeSave } : state;
    case "save-finished":
      return { ...state, beforeSave: null };
    case "applied":
      return state.settings
        ? { ...state, settings: { ...state.settings, catalogRefreshPending: false } }
        : state;
  }
}

export function groupDashboardModels(models: ModelInfo[]): Array<[string, ModelInfo[]]> {
  const groups = new Map<string, ModelInfo[]>();
  for (const model of models) {
    const rows = groups.get(model.provider);
    if (rows) rows.push(model);
    else groups.set(model.provider, [model]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function controlsCacheKey(apiBase: string): string {
  return `${CONTROLS_CACHE_PREFIX}${apiBase}`;
}

export function useDashboardData(apiBase: string, refreshEpoch = 0) {
  const liteRuntime = true;
  const { locale, t } = useI18n();
  // The hash is the source of truth for the active section (#dashboard, …).
  const [selectedSection, setSelectedSection] = useState<DashboardSection>(
    readDashboardSectionFromHash,
  );
  const [modelQuery, setModelQuery] = useState("");
  const [expandedProviders, setExpandedProviders] = useState<Set<string>>(new Set());
  const cachedControls = useMemo(
    () => readSessionListCache<CachedControls>(controlsCacheKey(apiBase)),
    [apiBase],
  );
  const cachedOverview = useMemo(
    () => readSessionListCache<CachedOverview>(`${OVERVIEW_CACHE_PREFIX}${apiBase}`),
    [apiBase],
  );
  const cachedUsage = useMemo(
    () => readSessionListCache<UsageSummary30d>(`${USAGE_CACHE_PREFIX}${apiBase}`),
    [apiBase],
  );
  const cachedStartup = useMemo(() => {
    const cached = readSessionListCache<{ status: StartupHealthStatus }>(
      `${STARTUP_CACHE_PREFIX}${apiBase}`,
    );
    return cached && ["native", "protected", "unverified"].includes(cached.status) ? cached : null;
  }, [apiBase]);
  const [health, setHealth] = useState<HealthData | null>(() => cachedOverview?.health ?? null);
  const [startupHealth, setStartupHealth] = useState<StartupHealthStatus | null>(
    () => cachedStartup?.status ?? null,
  );
  const [providers, setProviders] = useState<ProviderInfo[]>(() => cachedOverview?.providers ?? []);
  const [settingsState, dispatchSettings] = useReducer(dashboardSettingsReducer, {
    settings: cachedControls?.settings ?? null,
    beforeSave: null,
  });
  const { settings } = settingsState;
  const settingsSaving = settingsState.beforeSave !== null;
  const [sidecar, setSidecar] = useState<SidecarData | null>(() => cachedControls?.sidecar ?? null);
  const [shadowCall, setShadowCall] = useState<ShadowCallData | null>(
    () => cachedControls?.shadowCall ?? null,
  );
  const [usage30d, setUsage30d] = useState<UsageSummary30d | null>(() => cachedUsage);
  const [sidecarSaving, setSidecarSaving] = useState(false);
  const [sidecarCodexApply, setSidecarCodexApply] = useState<SidecarCodexApply | undefined>();
  const [shadowCallSaving, setShadowCallSaving] = useState(false);
  const [shadowCallError, setShadowCallError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [effortCapHelpOpen, setEffortCapHelpOpen] = useState(false);
  const [shadowCallHelpOpen, setShadowCallHelpOpen] = useState(false);
  const [injectionModel, setInjectionModel] = useState<string>("");
  const [injectionEffort, setInjectionEffort] = useState<string>("");
  const [injectionEfforts, setInjectionEfforts] = useState<string[]>([]);
  const [injectionAvailable, setInjectionAvailable] = useState<
    Array<{ provider: string; model: string; namespaced: string }>
  >([]);
  const [injectionSaving, setInjectionSaving] = useState(false);
  const [syncCodexSubagentDefaults, setSyncCodexSubagentDefaults] = useState(false);
  const [effortCap, setEffortCap] = useState<string>("");
  const [subagentEffortCap, setSubagentEffortCap] = useState<string>("");
  const [effortCapSaving, setEffortCapSaving] = useState(false);
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [projectConfigWarnings, setProjectConfigWarnings] = useState<ProjectCodexConfigGroup[]>([]);
  const settingsRequestEpochRef = useRef(0);
  const settingsMutationEpochRef = useRef(0);
  const settingsMutationInFlightRef = useRef(false);
  const shadowCallRequestEpochRef = useRef(0);
  const shadowCallMutationEpochRef = useRef(0);
  const shadowCallMutationInFlightRef = useRef(false);
  const [error, setError] = useState(false);
  const effortCapHelpTriggerRef = useRef<HTMLButtonElement>(null);
  const shadowCallHelpTriggerRef = useRef<HTMLButtonElement>(null);
  const effortCapHelpDialogRef = useModalDialog(effortCapHelpOpen, effortCapHelpTriggerRef);
  const shadowCallHelpDialogRef = useModalDialog(shadowCallHelpOpen, shadowCallHelpTriggerRef);

  useEffect(() => {
    const onHash = () => setSelectedSection(readDashboardSectionFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const startupHealthRef = useRef<StartupHealthStatus | null>(cachedStartup?.status ?? null);
  const startupProbeAuthoritativeRef = useRef(false);
  const startupTargetRef = useRef(apiBase);
  /** Bumped whenever the dedicated startup-health poll commits; core polls ignore older generations. */
  const startupHealthGenerationRef = useRef(0);
  const epochRefs = useRef<DashboardEpochRefs>({
    settingsRequestEpochRef,
    settingsMutationEpochRef,
    settingsMutationInFlightRef,
    shadowCallRequestEpochRef,
    shadowCallMutationEpochRef,
    shadowCallMutationInFlightRef,
  }).current;

  const startupHealthPoll = useKeyedClientResource(
    `dashboard-startup-health:${apiBase}`,
    [apiBase, refreshEpoch],
    (signal) => fetchStartupHealth(apiBase, signal),
    { pollMs: 30_000 },
  );

  useEffect(() => {
    if (startupTargetRef.current === apiBase) return;
    startupTargetRef.current = apiBase;
    startupHealthGenerationRef.current += 1;
    startupHealthRef.current = cachedStartup?.status ?? null;
    startupProbeAuthoritativeRef.current = false;
    setStartupHealth(startupHealthRef.current);
  }, [apiBase, cachedStartup]);

  /*
   * `/api/startup-health` answers instantly from a 30s cache and resolves the real probe in the
   * background, so a cold answer is a conservative placeholder. Waiting for the next 30s tick is
   * what made the chip look stuck until an unrelated action (refresh quota, tab hop) remounted it.
   * Re-ask in ~2s while the server says it is still working.
   */
  const startupHealthStale = !liteRuntime && probeNeedsFastRetry(startupHealthPoll.data);
  const refreshStartupHealth = startupHealthPoll.refresh;
  useEffect(() => {
    if (!startupHealthStale) return;
    const timer = window.setTimeout(() => {
      void refreshStartupHealth();
    }, STARTUP_HEALTH_STALE_RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [startupHealthStale, refreshStartupHealth]);

  // Wave 1: status/uptime/providers must not wait on injection-model / usage.
  const overviewPoll = useKeyedClientResource(
    `dashboard-overview:${apiBase}`,
    [apiBase, refreshEpoch],
    (signal) => fetchDashboardOverview(apiBase, signal),
    { pollMs: 5000 },
  );
  const overviewReady = health !== null || overviewPoll.data?.health != null;

  const sidecarPoll = useKeyedClientResource(
    `dashboard-sidecars:${apiBase}`,
    [apiBase, refreshEpoch],
    async (signal) => {
      const startupHealthGeneration = startupHealthGenerationRef.current;
      const data = await fetchDashboardSidecars(apiBase, signal, epochRefs, liteRuntime);
      return { ...data, startupHealthGeneration };
    },
    { pollMs: 5000 },
  );

  const settingsPoll = useKeyedClientResource(
    `dashboard-settings:${apiBase}`,
    [apiBase, refreshEpoch],
    async (signal) => {
      const startupHealthGeneration = startupHealthGenerationRef.current;
      const data = await fetchDashboardSettings(apiBase, signal, epochRefs);
      return { ...data, startupHealthGeneration };
    },
    { pollMs: 5000 },
  );

  // Wave 2: heavier peers start after overview commits (or session seed) to cut contention.
  const multiAgentPoll = useKeyedClientResource(
    `dashboard-multi-agent:${apiBase}`,
    [apiBase, refreshEpoch],
    (signal) => fetchDashboardMultiAgent(apiBase, signal),
    { pollMs: 5000, enabled: overviewReady },
  );

  const usagePoll = useKeyedClientResource(
    usageSummary30dResourceKey(apiBase),
    [apiBase, refreshEpoch],
    (signal) => fetchDashboardUsage(apiBase, signal),
    // 30d usage is documented ~5s cold; this shared key has four subscribers, so
    // every one of them carries the same raised deadline (mount-order independent).
    { enabled: overviewReady, pollMs: 60_000, deadlineMs: 60_000 },
  );

  const diagnosticsPoll = useKeyedClientResource(
    `dashboard-diagnostics:${apiBase}`,
    [apiBase, refreshEpoch],
    (signal) => fetchProjectConfigDiagnostics(apiBase, signal),
    { pollMs: PROJECT_CONFIG_DIAGNOSTICS_POLL_MS, enabled: overviewReady && !liteRuntime },
  );

  const modelsPoll = useKeyedClientResource(
    `dashboard-models:${apiBase}`,
    [apiBase, error, refreshEpoch],
    (signal) => fetchDashboardModels(apiBase, signal),
    { enabled: overviewReady && !error },
  );
  const modelsSurface = classifyDataSurface(modelsPoll, (data) => data.length === 0, true);
  const models = useMemo(() => modelsPoll.data ?? [], [modelsPoll.data]);
  const overviewFailure =
    overviewPoll.data?.failure ?? (overviewPoll.error !== undefined ? "unavailable" : undefined);
  const accessFailure = overviewFailure === "auth" || overviewFailure === "denied";
  const overviewBlocked = accessFailure || (!health && !!overviewFailure);

  /* eslint-disable react-hooks/set-state-in-effect -- mirror client-resource snapshots into mutable dashboard UI state that handlers also update */
  useEffect(() => {
    if (startupHealthPoll.data !== undefined) {
      const probe = startupHealthPoll.data;
      startupHealthGenerationRef.current += 1;
      setStartupHealth(probe.status);
      startupHealthRef.current = probe.status;
      startupProbeAuthoritativeRef.current = probe.status !== "error";
      // Never persist hard errors — a cold SWR miss used to poison revisits.
      // A stale answer is a placeholder too: caching it makes the next visit start from
      // the server's guess instead of asking again.
      if (probe.status !== "error" && probe.status !== "at-risk" && !probe.stale) {
        writeSessionListCache(`${STARTUP_CACHE_PREFIX}${apiBase}`, { status: probe.status });
      } else clearSessionListCache(`${STARTUP_CACHE_PREFIX}${apiBase}`);
    }
  }, [startupHealthPoll.data, apiBase]);

  useEffect(() => {
    const data = overviewPoll.data;
    if (!data) return;
    if (data.health) {
      setHealth(data.health);
      setProviders(data.providers);
      writeSessionListCache(`${OVERVIEW_CACHE_PREFIX}${apiBase}`, {
        health: data.health,
        providers: data.providers,
      });
    }
    setError(data.error);
  }, [overviewPoll.data, apiBase]);

  useEffect(() => {
    const data = multiAgentPoll.data;
    if (!data) return;
    if (data.injection) {
      setSyncCodexSubagentDefaults(data.injection.syncCodexSubagentDefaults);
      setInjectionModel(data.injection.injectionModel);
      setInjectionEffort(data.injection.injectionEffort);
      setInjectionEfforts(data.injection.injectionEfforts);
      setInjectionAvailable(data.injection.injectionAvailable);
    }
    if (data.effortCaps) {
      setEffortCap(data.effortCaps.effortCap);
      setSubagentEffortCap(data.effortCaps.subagentEffortCap);
    }
  }, [multiAgentPoll.data]);

  useEffect(() => {
    const data = sidecarPoll.data;
    if (!data) return;
    setSidecar(data.sidecar);
    if (data.shadowCall !== undefined) setShadowCall(data.shadowCall);
    const prev = readSessionListCache<CachedControls>(controlsCacheKey(apiBase)) ?? {};
    writeSessionListCache(controlsCacheKey(apiBase), {
      ...prev,
      sidecar: data.sidecar,
      ...(data.shadowCall !== undefined ? { shadowCall: data.shadowCall } : {}),
    });
  }, [sidecarPoll.data, apiBase]);

  useEffect(() => {
    const data = settingsPoll.data;
    if (!data) return;
    if (data.settings !== undefined) {
      dispatchSettings({ type: "polled", settings: data.settings });
    }
    // Latest-wins: only seed from settings when no newer dedicated probe has committed
    // while this settings poll was in flight. Always merge against the live ref.
    if (
      data.startupHealthSeed !== undefined &&
      data.startupHealthGeneration === startupHealthGenerationRef.current
    ) {
      const previous = startupHealthRef.current;
      const merged = seedStartupHealthFromSettings(
        previous,
        data.startupHealthSeed,
        startupProbeAuthoritativeRef.current,
      );
      setStartupHealth(merged);
      startupHealthRef.current = merged;
      if (
        merged &&
        merged !== "at-risk" &&
        merged !== "error" &&
        !data.startupHealthSeed?.diagnosticStale
      )
        writeSessionListCache(`${STARTUP_CACHE_PREFIX}${apiBase}`, { status: merged });
      else if (merged === "at-risk" || merged === "error")
        clearSessionListCache(`${STARTUP_CACHE_PREFIX}${apiBase}`);
    }
  }, [settingsPoll.data, apiBase]);

  // Cache the merged UI state, including preference saves and successful applies.
  // Raw GET settings cannot replace the local application receipt on a revisit.
  useEffect(() => {
    if (!settings) return;
    const prev = readSessionListCache<CachedControls>(controlsCacheKey(apiBase)) ?? {};
    writeSessionListCache(controlsCacheKey(apiBase), { ...prev, settings });
  }, [settings, apiBase]);

  useEffect(() => {
    if (usagePoll.data !== undefined) {
      setUsage30d(usagePoll.data);
      writeSessionListCache(`${USAGE_CACHE_PREFIX}${apiBase}`, usagePoll.data);
    }
  }, [usagePoll.data, apiBase]);

  useEffect(() => {
    if (diagnosticsPoll.data) setProjectConfigWarnings(diagnosticsPoll.data);
  }, [diagnosticsPoll.data]);

  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(
    () => () => {
      settingsRequestEpochRef.current += 1;
      shadowCallRequestEpochRef.current += 1;
    },
    [],
  );

  const grouped = useMemo(() => groupDashboardModels(models), [models]);
  const filteredGroups = useMemo(() => {
    const q = modelQuery.trim().toLowerCase();
    if (!q) return grouped;
    const out: Array<[string, ModelInfo[]]> = [];
    for (const [provider, rows] of grouped) {
      const hits = rows.filter(
        (m) => m.id.toLowerCase().includes(q) || provider.toLowerCase().includes(q),
      );
      if (hits.length > 0) out.push([provider, hits]);
    }
    return out;
  }, [grouped, modelQuery]);
  const sidecarModels = useMemo(() => {
    // Server-computed runnable set when present (#2188); legacy union otherwise.
    // The shared SidecarSetting type admits vision's "routed", which the
    // web-search picker cannot carry — narrow it away for this card.
    const webBackend = sidecar?.webSearch?.backend;
    return webSearchModelOptionsForPicker(
      sidecar?.webSearchModels,
      models,
      sidecar?.webSearch?.model,
      webBackend === "routed" ? undefined : webBackend,
    );
  }, [models, sidecar?.webSearchModels, sidecar?.webSearch]);
  const visionModels = useMemo(
    () =>
      visionModelOptions(
        sidecar?.visionModels,
        models,
        sidecar?.vision?.model,
        sidecar?.vision?.backend,
      ),
    [sidecar?.visionModels, models, sidecar?.vision],
  );

  const saveSidecar = async (patch: SidecarPatch) => {
    if (!sidecar || sidecarSaving) return;
    patch = sidecarPatchForSave(sidecar, patch);
    const previous = sidecar;
    const next = {
      webSearch: mergeSidecarSetting(sidecar.webSearch, patch.webSearch),
      vision: mergeSidecarSetting(sidecar.vision, patch.vision),
      ...(sidecar.visionModels ? { visionModels: sidecar.visionModels } : {}),
      ...(sidecar.webSearchModels ? { webSearchModels: sidecar.webSearchModels } : {}),
    };
    setSidecarSaving(true);
    setSidecar(next);
    try {
      const res = await fetch(`${apiBase}/api/sidecar-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const data = await requireJson<SidecarData>(res, "save failed");
      // The Codex-side write is a separate outcome from the stored switch: it can be refused
      // while the setting is saved, and the card has to say so instead of implying it happened.
      // A save that did not move this switch answers `not_requested` about a file it never
      // touched, so it must not clear an earlier failure.
      setSidecarCodexApply((previousReport) =>
        nextSidecarCodexApply(previousReport, data.codexWebSearch),
      );
      setSidecar({
        webSearch: data.webSearch,
        vision: data.vision,
        ...(data.visionModels ? { visionModels: data.visionModels } : {}),
        ...(data.webSearchModels ? { webSearchModels: data.webSearchModels } : {}),
      });
      const prev = readSessionListCache<CachedControls>(controlsCacheKey(apiBase)) ?? {};
      writeSessionListCache(controlsCacheKey(apiBase), {
        ...prev,
        sidecar: {
          webSearch: data.webSearch,
          vision: data.vision,
          ...(data.visionModels ? { visionModels: data.visionModels } : {}),
          ...(data.webSearchModels ? { webSearchModels: data.webSearchModels } : {}),
        },
      });
    } catch {
      setSidecar(previous);
      // The request failed before any answer existed, so it says nothing about the Codex file:
      // an outstanding report stays until a write that ran or a successful sync settles it.
    } finally {
      setSidecarSaving(false);
    }
  };

  async function saveShadowCall(patch: Partial<ShadowCallData>) {
    if (!shadowCall || shadowCallSaving) return;
    const previous = shadowCall;
    const updated = { ...shadowCall, ...patch };
    setShadowCallSaving(true);
    setShadowCallError(null);
    shadowCallMutationInFlightRef.current = true;
    setShadowCall(updated);
    try {
      const res = await fetch(`${apiBase}/api/shadow-call-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        setShadowCall(previous);
        const body = (await res.json().catch(() => null)) as { code?: unknown } | null;
        setShadowCallError(t(shadowCallSaveErrorKey(body?.code)));
        return;
      }
      shadowCallMutationEpochRef.current += 1;
    } catch {
      setShadowCall(previous);
      setShadowCallError(t("models.networkError"));
    } finally {
      shadowCallMutationInFlightRef.current = false;
      setShadowCallSaving(false);
    }
  }

  const saveInjection = async (patch: {
    syncCodexSubagentDefaults?: boolean;
    model?: string | null;
    effort?: string | null;
  }) => {
    if (injectionSaving) return;
    setInjectionSaving(true);
    try {
      const res = await fetch(`${apiBase}/api/injection-model`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error("injection save failed");
      const getRes = await fetch(`${apiBase}/api/injection-model`);
      const data = await requireJson<{
        syncCodexSubagentDefaults?: boolean;
        model?: string | null;
        effort?: string | null;
        efforts?: string[];
        available?: Array<{ provider: string; model: string; namespaced: string }>;
      }>(getRes);
      const normalized = normalizeInjectionSelection(data);
      setSyncCodexSubagentDefaults(normalized.syncCodexSubagentDefaults);
      setInjectionModel(normalized.injectionModel);
      setInjectionEffort(normalized.injectionEffort);
      if (Array.isArray(data.efforts)) setInjectionEfforts(data.efforts);
      if (Array.isArray(data.available)) setInjectionAvailable(data.available);
    } catch {
      /* keep the last committed UI state */
    } finally {
      setInjectionSaving(false);
    }
  };

  const toggleCodexSetting = async (key: CodexPreference) => {
    if (!settings || settingsSaving || syncing) return;
    const next = !(settings[key] ?? key === "codexAutoStart");
    settingsMutationInFlightRef.current = true;
    dispatchSettings({ type: "save-started", key, value: next });
    try {
      const res = await fetch(`${apiBase}/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: next }),
      });
      const data = await requireJson<SettingsData>(res, "save failed");
      settingsMutationEpochRef.current += 1;
      dispatchSettings({ type: "save-succeeded", key, settings: data });
      if (!liteRuntime && (key === "codexDesktopAuthless" || key === "codexClientCompaction"))
        await runSync();
    } catch {
      dispatchSettings({ type: "save-failed" });
      setError(true);
    } finally {
      settingsMutationInFlightRef.current = false;
      dispatchSettings({ type: "save-finished" });
    }
  };

  const toggleCodexAutoStart = () => toggleCodexSetting("codexAutoStart");
  const toggleCodexDesktopAuthless = () => toggleCodexSetting("codexDesktopAuthless");
  const toggleCodexClientCompaction = () => toggleCodexSetting("codexClientCompaction");

  // Clears the sync result/error in this hook. The dashboard toast owns its own dismissal
  // timer but must publish the dismissal here: syncResult/syncError live above the dashboard
  // tabs, so a component-local flag alone would let a stale result remount as a fresh toast
  // after the Overview panel unmounts and comes back.
  const clearSyncFeedback = useCallback(() => {
    setSyncResult(null);
    setSyncError(null);
  }, []);

  const runSync = async () => {
    if (syncing) return;
    setSyncing(true);
    setSyncResult(null);
    setSyncError(null);
    try {
      const res = await fetch(`${apiBase}/api/sync`, { method: "POST" });
      const data = await requireJson<
        SyncResult & { projectConfigGrouped?: ProjectCodexConfigGroup[] }
      >(res, "sync failed");
      setSyncResult(data);
      if (data.ok && data.status === "applied") {
        dispatchSettings({ type: "applied" });
        // A successful sync rewrites the Codex config from the stored settings, which is exactly
        // the write the sidecar card was still warning about.
        setSidecarCodexApply(undefined);
      }
      if (data.projectConfigGrouped) setProjectConfigWarnings(data.projectConfigGrouped);
    } catch (err) {
      setSyncError(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  };

  return {
    apiBase,
    liteRuntime,
    locale,
    t,
    selectedSection,
    setSelectedSection,
    modelQuery,
    setModelQuery,
    expandedProviders,
    setExpandedProviders,
    health,
    startupHealth,
    providers,
    models,
    settings,
    sidecar,
    shadowCall,
    usage30d,
    usageLoading: classifyDataSurface(usagePoll, () => false, true).showSkeleton && !usage30d,
    healthLoading: !health && !overviewBlocked,
    overviewBlocked,
    overviewRefreshing: overviewPoll.refreshing,
    modelsSurface,
    refreshModels: modelsPoll.refresh,
    settingsLoading: classifyDataSurface(settingsPoll, () => false, true).showSkeleton,
    sidecarLoading: classifyDataSurface(sidecarPoll, () => false, true).showSkeleton,
    multiAgentLoading: classifyDataSurface(multiAgentPoll, () => false, true).showSkeleton,
    injectionReady: !!multiAgentPoll.data?.injection,
    effortCapsReady: !!multiAgentPoll.data?.effortCaps,
    sidecarSaving,
    shadowCallSaving,
    shadowCallError,
    settingsSaving,
    syncing,
    effortCapHelpOpen,
    setEffortCapHelpOpen,
    shadowCallHelpOpen,
    setShadowCallHelpOpen,
    injectionModel,
    injectionEffort,
    injectionEfforts,
    injectionAvailable,
    injectionSaving,
    syncCodexSubagentDefaults,
    saveInjection,
    effortCap,
    subagentEffortCap,
    effortCapSaving,
    setEffortCap,
    setSubagentEffortCap,
    setEffortCapSaving,
    syncResult,
    syncError,
    projectConfigWarnings,
    error: error || !!overviewFailure,
    connectionFailure: overviewFailure,
    refreshDashboard: overviewPoll.refresh,
    effortCapHelpTriggerRef,
    shadowCallHelpTriggerRef,
    effortCapHelpDialogRef,
    shadowCallHelpDialogRef,
    filteredGroups,
    sidecarModels,
    visionModels,
    sidecarCodexApply,
    saveSidecar,
    saveShadowCall,
    toggleCodexAutoStart,
    toggleCodexDesktopAuthless,
    toggleCodexClientCompaction,
    runSync,
    clearSyncFeedback,
  };
}
