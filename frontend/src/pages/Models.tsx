import { createBoundedFetch } from "@/bounded-fetch";
import { Button } from "@/components/button";
import { clientApplicationWarning } from "@/contracts/application";
import type { ApplicationReceipt } from "@/contracts/channels";
import { useChannelCopy } from "@/features/channels/copy";
import { CatalogModelRow } from "@/features/models/CatalogModelRow";
import { ContextSettingsDialog } from "@/features/models/ContextSettingsDialog";
import { CustomModelDialog } from "@/features/models/CustomModelDialog";
import { useCatalogClientStatus } from "@/features/models/use-catalog-client-status";
import { useModelDirectory } from "@/features/models/use-model-directory";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { confirmAction } from "../action-dialogs";
import { CodexStaleBanner } from "../components/codex-stale-banner";
import { DataSurfaceSkeleton } from "../components/data-surface";
import ErrorBoundary from "../components/ErrorBoundary";
import { HelpPopover } from "../components/help-popover";
import { ModelDiscoveryStatus } from "../components/model-discovery-status";
import ModelSettingsDialog from "../components/ModelSettingsDialog";
import { PageHeader } from "../components/page-header";
import { PlannedFeature } from "../components/planned-feature";
import { ModelsAbout } from "../components/v2-models/models-about";
import { ModelsProviderRail } from "../components/v2-models/models-provider-rail";
import { ProviderMark } from "../components/v2-models/provider-mark";
import { readJsonIfOk, readJsonOrThrow } from "../fetch-json";
import type { TKey } from "../i18n/shared";
import { useI18n } from "../i18n/shared";
import { IconAlert, IconBoxes, IconCheck, IconChevron, IconPencil, IconRefresh } from "../icons";
import {
  modelVisible as savedModelVisible,
  type ModelVisibilityScope,
  type ModelVisibilityTarget,
  type ProviderModelMap,
} from "../model-visibility";
import { buildProviderModelGroups, type ProviderModelGroup } from "../models-groups";
import { providerDisplaySlug } from "../provider-icons";
import { EmptyState, Notice, Select, Switch, type NoticeTone } from "../ui";
import { useModelVisibility } from "../use-model-visibility";
import { editModelAlias, editProviderAlias } from "./models-alias-editing";
import { DiscoveryDependencyHint, EmptyProviderHint } from "./models-provider-hints";
import { ModelsReloadFocusBoundary } from "./models-reload-focus-boundary";
import { CustomModelsSummary, ModelsSettingsPanel, SettingsRow } from "./models-settings-panel";
import { modelsSettingsSummary } from "./models-settings-summary";
import {
  CAP_OPTION_SET,
  CAP_OPTIONS,
  CUSTOM_OPTION,
  discoveryFailureLabel,
  filterFreeModelRows,
  fmtK,
  freeOnlyInForce,
  modelPricingKnown,
  NATIVE_CAP_OPTION_SET,
  NATIVE_CAP_OPTIONS,
  PAGE,
  readCollapsedProviders,
  writeCollapsedProviders,
  type ModelPresetView,
  type ModelRow,
  type ProviderContextCapsResponse,
} from "./models-shared";
import {
  modelsPanelDomId,
  modelsTabDomId,
  readModelsTab,
  selectModelsTab,
  type ModelsTab,
} from "./models-tab";
import { ModelsTabStrip } from "./models-tab-strip";

/** One plain lead line per tab; a longer description (if any) opens from the "?" beside it. */
const SUBTITLE_TKEY: Record<ModelsTab, [lede: TKey, about?: TKey]> = {
  catalog: ["modelsV2.ledeCatalog", "models.subtitle"],
  routing: ["models.subtitle.routing"],
};

interface ModelDiscoveryView {
  ambiguousDisabledSelectors?: string[];
  policy: "on" | "off";
  providers: Record<string, "on" | "off" | "inherit">;
  recentArrivals: Record<string, Array<{ id: string; at: string; state: string }>>;
}

interface AliasView {
  providers: Record<string, string>;
  models: Record<
    string,
    Record<string, { alias: string; source: "user" | "builtin"; stale?: boolean }>
  >;
  defaults: { global: boolean; providers: Record<string, boolean> };
}

export default function Models({
  apiBase,
  restartEpoch = 0,
  reportRestart,
}: {
  apiBase: string;
  restartEpoch?: number;
  reportRestart: (message: string, tone: NoticeTone) => void;
}) {
  const { appServerState, reloadHeadRef, reloadBannerRef, codexRestarting, handleCodexRestart } =
    useCatalogClientStatus(apiBase, restartEpoch, reportRestart);
  /*
   * Tab state. The hash is the source of truth, so refresh, bookmark, and
   * Back/Forward keep the choice — same contract as `#logs` / `#logs/debug`.
   *
   * Panels mount lazily and then STAY mounted, hidden, so a half-typed combo draft
   * survives a tab hop. The mounted set accumulates in the handler rather than an
   * effect: an effect would cost a second render pass on every switch for a value both
   * callers already know.
   */
  const [tab, setTab] = useState<ModelsTab>(readModelsTab);
  const [mounted, setMounted] = useState<ReadonlySet<ModelsTab>>(() => new Set([readModelsTab()]));

  const activateTab = useCallback((next: ModelsTab) => {
    setTab(next);
    setMounted((current) => (current.has(next) ? current : new Set([...current, next])));
  }, []);

  useEffect(() => {
    const syncFromHash = () => activateTab(readModelsTab());
    window.addEventListener("hashchange", syncFromHash);
    window.addEventListener("popstate", syncFromHash);
    return () => {
      window.removeEventListener("hashchange", syncFromHash);
      window.removeEventListener("popstate", syncFromHash);
    };
  }, [activateTab]);

  const selectTab = useCallback(
    (next: ModelsTab) => {
      // Deliberate navigation: push a history entry so Back/Forward restore the tab.
      selectModelsTab(next);
      activateTab(next);
    },
    [activateTab],
  );

  const catalogActive = tab === "catalog";
  const routingCount = null;

  const c = useChannelCopy();
  const { t } = useI18n();
  const directory = useModelDirectory(apiBase, catalogActive);
  const {
    models,
    providers,
    disabled,
    selectedModels,
    contextCaps,
    contextCapValues,
    contextCapValue,
    setContextCaps,
    setContextCapValues,
    setContextCapValue,
    cached,
    catalogState,
    catalogResource,
    load,
  } = directory;
  const [search, setSearch] = useState<Record<string, string>>({});
  // Per-provider Free-only narrowing (#3666). Session UX, not persisted config: it answers
  // "what can I run for nothing right now", which is a question about this sitting, and the
  // provider list it applies to changes underneath a stored value.
  const [freeOnly, setFreeOnly] = useState<Record<string, boolean>>({});
  const [limit, setLimit] = useState<Record<string, number>>({});
  const [customCap, setCustomCap] = useState("");
  const [showCustom, setShowCustom] = useState(false);
  const [providerCapCustomOpen, setProviderCapCustomOpen] = useState<Record<string, boolean>>({});
  const [providerCapCustomDraft, setProviderCapCustomDraft] = useState<Record<string, string>>({});
  const initialCollapsed = readCollapsedProviders();
  const [collapsed, setCollapsed] = useState<Set<string>>(() => initialCollapsed ?? new Set());
  const needsDefaultCollapseRef = useRef(initialCollapsed === null);
  const [status, setStatus] = useState("");
  const [integrationWarning, setIntegrationWarning] = useState<string>();
  const reportClientApplication = (receipt?: ApplicationReceipt) => {
    if (receipt) {
      setIntegrationWarning(clientApplicationWarning(receipt, t("integrations.catalogSyncFailed")));
    }
  };
  const [ok, setOk] = useState(false);
  // Feedback generation: a repeated identical message (same success string, same validation
  // error) must still re-arm the toast timer. Clearing `status` alone is not enough — a
  // second identical value bails out of React's state diff, so the old timer would dismiss
  // the new toast early. Every publish bumps the generation.
  const [feedbackGen, setFeedbackGen] = useState(0);
  const publishFeedback = useCallback((nextOk: boolean, message: string) => {
    setOk(nextOk);
    setStatus(message);
    setFeedbackGen((g) => g + 1);
  }, []);
  // Transient action feedback as a fixed toast: appearing or auto-clearing it never shifts
  // the workspace below (the old inline Notice pushed the whole model grid down by its
  // height on every apply). The timer itself just clears the status again.
  useEffect(() => {
    if (!status) return;
    const holdMs = ok ? 6000 : 8000;
    const timer = setTimeout(() => setStatus(""), holdMs);
    return () => clearTimeout(timer);
  }, [status, ok, feedbackGen]);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const visibility = useModelVisibility(apiBase, {
    onQueued: () => {
      directory.invalidate();
      setStatus("");
    },
    onBusy: (value) => {
      directory.invalidate();
      directory.setMutation(value);
      busyRef.current = value;
      setBusy(value);
    },
    onResponse: (body) => {
      reportClientApplication(body?.catalogRefresh);
    },
    refresh: (signal) => load(true, signal),
    onSettled: (error) => {
      setOk(!error);
      setStatus(t(error ?? "models.applied"));
    },
  });
  const modelVisible = (
    selected: ProviderModelMap,
    provider: string,
    id: string,
    native: boolean,
    blocked: boolean,
  ) =>
    visibility.visible(
      provider,
      id,
      native,
      savedModelVisible(selected, provider, id, native, blocked),
    );
  // #2465: per-provider model-preset state. Keyed by provider so one card's busy state cannot
  // freeze the others.
  const [presets, setPresets] = useState<Record<string, ModelPresetView>>({});
  const [modelDiscovery, setModelDiscovery] = useState<ModelDiscoveryView | null>(null);
  const [aliases, setAliases] = useState<AliasView>({
    providers: {},
    models: {},
    defaults: { global: false, providers: {} },
  });
  const [showAliases, setShowAliases] = useState(false);
  const [presetBusy, setPresetBusy] = useState<string | null>(null);
  const [customModalOpen, setCustomModalOpen] = useState(false);
  const [settingsModel, setSettingsModel] = useState<ModelRow | null>(null);

  const reloadAliases = useCallback(
    async (signal?: AbortSignal) => {
      const response = await fetch(`${apiBase}/api/aliases`, { signal });
      const data = await readJsonIfOk<AliasView>(response);
      if (data && !signal?.aborted) setAliases(data);
    },
    [apiBase],
  );
  useEffect(() => {
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Start and cancel the alias request with this API base.
    void reloadAliases(controller.signal).catch(() => {
      if (!controller.signal.aborted) {
        publishFeedback(false, t("models.surface.couldNotLoadModelAliases"));
      }
    });
    return () => controller.abort();
  }, [reloadAliases, publishFeedback, t]);

  const aliasEditingDeps = {
    apiBase,
    t,
    reloadAliases,
    publishFeedback,
    pendingMessage: c("pendingApply"),
  };
  const saveProviderAlias = (provider: string) =>
    editProviderAlias(provider, aliases.providers[provider] ?? "", aliasEditingDeps);
  const saveModelAlias = (provider: string, model: string) =>
    editModelAlias(
      provider,
      model,
      aliases.models[provider]?.[model]?.alias ?? "",
      aliasEditingDeps,
    );

  const setDefaultAliases = async (enabled: boolean, provider?: string) => {
    const response = await fetch(`${apiBase}/api/default-aliases`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled, ...(provider ? { provider } : {}) }),
    });
    try {
      const result = await readJsonOrThrow<{ catalogRefresh: ApplicationReceipt }>(response);
      reportClientApplication(result?.catalogRefresh);
      await reloadAliases();
      publishFeedback(true, t("models.aliasSaved"));
    } catch (error) {
      publishFeedback(false, error instanceof Error ? error.message : t("models.saveFailed"));
    }
  };
  const [customModalProvider, setCustomModalProvider] = useState("");
  const [contextGroup, setContextGroup] = useState<ProviderModelGroup<ModelRow> | null>(null);
  // App owns the in-session view mode; fallback to persisted mode for isolated renders/tests.
  const [selectedProvider, setSelectedProvider] = useState<string | null>(() =>
    new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("provider"),
  );
  useEffect(() => {
    const syncProvider = () =>
      setSelectedProvider(
        new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("provider"),
      );
    window.addEventListener("hashchange", syncProvider);
    return () => window.removeEventListener("hashchange", syncProvider);
  }, []);

  useEffect(() => {
    if (!catalogActive) return;
    const timer = window.setTimeout(() => {
      void loadPresets();
      void loadModelDiscovery();
      void reloadAliases();
    }, 0);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Local directory settings reload when the catalog becomes visible.
  }, [catalogActive, apiBase]);

  const groups = useMemo(() => buildProviderModelGroups(models, providers), [models, providers]);

  /*
   * The catalog count is only honest once a seed or a real response has landed. With
   * the catalog gated, a cold load straight to `#models/combos` never fetches it, and
   * rendering "0/0" would present unknown as fact.
   */
  const catalogCountReady = models.length > 0 || catalogState.data !== undefined;

  const openContextSettings = (group: ProviderModelGroup<ModelRow>) => setContextGroup(group);

  // One-shot default collapse. It stays an effect on `groups` so CACHED groups collapse
  // immediately on first paint, even when revalidation is slow or fails; moving it into
  // the load() success path would render cached providers expanded and leave them
  // expanded whenever the refresh errors.
  useEffect(() => {
    if (!needsDefaultCollapseRef.current) return;
    if (groups.length === 0) return;
    needsDefaultCollapseRef.current = false;
    const all = new Set(groups.map((group) => group.provider));
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Initialize collapsed state as soon as cached or fetched groups are available.
    setCollapsed(all);
    writeCollapsedProviders(all);
  }, [groups]);

  const effectiveVisibleCount = useMemo(() => {
    if (!selectedModels) return 0;
    return models.filter((model) =>
      modelVisible(
        selectedModels,
        model.provider,
        model.id,
        model.native === true,
        disabled.has(model.namespaced),
      ),
    ).length;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- modelVisible closes over the override map, which is tracked directly instead of the render-local function.
  }, [disabled, models, selectedModels, visibility.overrides]);

  /*
   * Quiet per-tab counts. A count is omitted, never zeroed, while it is unknown: the
   * panels report theirs up once mounted, and a tab that has never been opened has
   * nothing truthful to say.
   */
  const tabMeta = useMemo(
    () => ({
      catalog: catalogCountReady
        ? t("models.active", { active: effectiveVisibleCount, total: models.length })
        : undefined,
      routing: routingCount === null ? undefined : String(routingCount),
    }),
    [catalogCountReady, effectiveVisibleCount, models.length, routingCount, t],
  );

  const applyVisibility = async (
    scope: ModelVisibilityScope,
    provider: string,
    targets: ModelVisibilityTarget[],
    enabled: boolean,
  ) => {
    if (busyRef.current && !visibility.isRunning()) return;
    visibility.enqueue(scope, provider, targets, enabled);
  };

  const toggleProviderCap = async (provider: string) => {
    setBusy(true);
    busyRef.current = true;
    setStatus("");
    // Send the desired next state, not the current one: clicking the switch turns a
    // currently-unset cap on (enabled: true) and a currently-set cap off (enabled: false).
    const enabled = contextCaps[provider] === undefined;
    try {
      const r = await fetch(`${apiBase}/api/provider-context-caps`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, enabled }),
      });
      try {
        const data = await readJsonOrThrow<
          ProviderContextCapsResponse & { catalogRefresh: ApplicationReceipt }
        >(r, t("models.capSaveFailed"));
        reportClientApplication(data?.catalogRefresh);
        setContextCaps(data?.caps ?? {});
        setContextCapValues(data?.values ?? data?.caps ?? {});
        setOk(true);
        setStatus(t("models.capApplied"));
        await load(true);
      } catch (e) {
        setOk(false);
        setStatus(e instanceof Error ? e.message : t("models.capSaveFailed"));
      }
    } catch {
      setOk(false);
      setStatus(t("models.networkError"));
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
  };
  const toggleCollapse = (p: string) => {
    setCollapsed((prev) => {
      const n = new Set(prev);
      if (n.has(p)) n.delete(p);
      else n.add(p);
      writeCollapsedProviders(n);
      return n;
    });
  };
  const setAllCollapsed = (collapse: boolean) => {
    setCollapsed(() => {
      const n = collapse ? new Set(groups.map((group) => group.provider)) : new Set<string>();
      writeCollapsedProviders(n);
      return n;
    });
  };

  const putCap = async (body: Record<string, unknown>) => {
    setBusy(true);
    busyRef.current = true;
    setStatus("");
    try {
      const r = await fetch(`${apiBase}/api/provider-context-caps`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      try {
        const data = await readJsonOrThrow<
          ProviderContextCapsResponse & { catalogRefresh: ApplicationReceipt }
        >(r, t("models.capSaveFailed"));
        reportClientApplication(data?.catalogRefresh);
        if (typeof data?.value === "number" && Number.isFinite(data.value) && data.value > 0)
          setContextCapValue(data.value);
        setContextCaps(data?.caps ?? {});
        setContextCapValues(data?.values ?? data?.caps ?? {});
        setOk(true);
        setStatus(t("models.capApplied"));
        await load(true);
      } catch (e) {
        setOk(false);
        setStatus(e instanceof Error ? e.message : t("models.capSaveFailed"));
      }
    } catch {
      setOk(false);
      setStatus(t("models.networkError"));
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
  };

  const allCapped = useMemo(() => {
    // Cap aggregate counts routed providers only; the single native group has no cap
    // switch. Zero-row routed providers are included: they can still hold a per-provider
    // cap (e.g. a custom model added later), and excluding them would let "set all"
    // silently overwrite that cap when the global value changes. Saved caps of providers
    // that are no longer in `groups` (e.g. disabled after receiving a custom cap) are
    // also counted: the management API rewrites every key in providerContextCaps when
    // setAll is true, so any saved cap that differs from the current value must keep the
    // aggregate off.
    const routed = groups.filter((group) => !group.native);
    return (
      routed.length > 0 &&
      routed.every((group) => contextCaps[group.provider] === contextCapValue) &&
      Object.keys(contextCaps).every((key) => contextCaps[key] === contextCapValue)
    );
  }, [groups, contextCaps, contextCapValue]);

  const setGlobalCap = (value: number) => {
    if (!Number.isSafeInteger(value) || value <= 0) return;
    // Only when "apply to every routed provider" is checked does the new value re-point every
    // provider; otherwise it just becomes the default for future toggles and providers keep
    // their own values.
    void putCap(allCapped ? { value, setAll: true } : { value });
  };

  const onSelectProviderCap = (provider: string, raw: string) => {
    if (raw === CUSTOM_OPTION) {
      setProviderCapCustomOpen((prev) => ({ ...prev, [provider]: true }));
      setProviderCapCustomDraft((prev) => ({
        ...prev,
        [provider]: String(contextCaps[provider] ?? contextCapValues[provider] ?? contextCapValue),
      }));
      return;
    }
    setProviderCapCustomOpen((prev) => ({ ...prev, [provider]: false }));
    const value = Number(raw);
    if (Number.isSafeInteger(value) && value > 0 && value !== contextCaps[provider]) {
      void putCap({ provider, enabled: true, value });
    }
  };

  const applyProviderCustomCap = (provider: string) => {
    const value = Number((providerCapCustomDraft[provider] ?? "").replace(/[_,\s]/g, ""));
    // Fractional values are rejected (the server floors, so 0.5 would silently become 0).
    // The editor stays open when validation fails.
    if (!Number.isSafeInteger(value) || value <= 0) {
      publishFeedback(false, t("models.capSaveFailed"));
      return;
    }
    setProviderCapCustomOpen((prev) => ({ ...prev, [provider]: false }));
    void putCap({ provider, enabled: true, value });
  };

  const onSelectCap = (raw: string) => {
    if (raw === CUSTOM_OPTION) {
      setShowCustom(true);
      setCustomCap(String(contextCapValue));
      return;
    }
    setShowCustom(false);
    const value = Number(raw);
    if (Number.isSafeInteger(value) && value > 0 && value !== contextCapValue) setGlobalCap(value);
  };

  const applyCustomCap = () => {
    const value = Number(customCap.replace(/[_,\s]/g, ""));
    if (!Number.isSafeInteger(value) || value <= 0) {
      publishFeedback(false, t("models.capSaveFailed"));
      return;
    }
    setShowCustom(false);
    setGlobalCap(value);
  };

  const setAll = () => {
    void putCap({ setAll: !allCapped });
  };

  /**
   * #2465: load the per-provider preset preview. Rules are evaluated server-side against the
   * CURRENT catalog, so the count shown is the count an apply would produce.
   */
  const loadPresets = async () => {
    try {
      const bounded = createBoundedFetch(15_000);
      const r = await fetch(`${apiBase}/api/model-presets`, { signal: bounded.signal });
      const data = await readJsonIfOk<{ providers?: Record<string, ModelPresetView> }>(r);
      setPresets(data?.providers ?? {});
    } catch {
      // A preset preview is decoration on top of a working Models page; failing to load it must
      // not take the page down.
      setPresets({});
    }
  };

  const loadModelDiscovery = async () => {
    try {
      const r = await fetch(`${apiBase}/api/model-discovery`);
      setModelDiscovery((await readJsonIfOk<ModelDiscoveryView>(r)) ?? null);
    } catch {
      setModelDiscovery(null);
    }
  };

  const saveModelDiscovery = async (policy: "on" | "off" | "inherit", provider?: string) => {
    try {
      const response = await fetch(`${apiBase}/api/model-discovery`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ policy, provider: provider ?? null }),
      });
      const result = await readJsonOrThrow<{ catalogRefresh: ApplicationReceipt }>(response);
      reportClientApplication(result?.catalogRefresh);
      await Promise.all([loadModelDiscovery(), load()]);
      publishFeedback(true, t("models.applied"));
    } catch (error) {
      publishFeedback(false, error instanceof Error ? error.message : t("models.saveFailed"));
    }
  };

  const applyPreset = async (
    provider: string,
    mode: "preset" | "all",
    replacing?: { presetCount: number },
  ) => {
    // Consent lives with the write, not with the button, so every caller is gated.
    if (
      replacing &&
      !(await confirmAction({
        message: t("models.presetConfirmReplace", { count: String(replacing.presetCount) }),
        tone: "danger",
      }))
    )
      return;
    if (!directory.beginMutation()) return;
    setPresetBusy(provider);
    setBusy(true);
    busyRef.current = true;
    try {
      const bounded = createBoundedFetch(30_000);
      const r = await fetch(`${apiBase}/api/model-presets`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ provider, mode }),
        signal: bounded.signal,
      });
      const res = await readJsonOrThrow<{
        fallback?: string;
        selected?: string[];
        catalogRefresh?: ApplicationReceipt;
      }>(r, t("models.saveFailed"));
      if (!res) throw new Error(t("models.saveFailed"));
      if (res.fallback === "preset-empty") {
        // Never silently narrow to nothing: the server kept the previous selection, so say so
        // rather than showing a success that changed nothing.
        publishFeedback(false, t("models.presetEmpty", { provider }));
      } else {
        reportClientApplication(res.catalogRefresh);
        publishFeedback(
          true,
          mode === "all"
            ? t("models.presetClearedToast", { provider })
            : t("models.presetAppliedToast", {
                provider,
                count: String(res.selected?.length ?? 0),
              }),
        );
      }
      await Promise.all([loadPresets(), load()]);
    } catch (error) {
      publishFeedback(false, error instanceof Error ? error.message : String(error));
    } finally {
      setPresetBusy(null);
      setBusy(false);
      busyRef.current = false;
      directory.setMutation(false);
    }
  };

  const deleteCustomModel = async (id: string, name: string) => {
    if (
      !(await confirmAction({
        message: t("models.customDeleteConfirm", { name }),
        confirmLabel: t("common.delete"),
        tone: "danger",
      }))
    )
      return;
    try {
      const r = await fetch(`${apiBase}/api/custom-models/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      if (r.ok) {
        const result = await readJsonOrThrow<{ catalogRefresh: ApplicationReceipt }>(r);
        reportClientApplication(result?.catalogRefresh);
        publishFeedback(true, t("models.customDeleted"));
        await load(true);
      } else {
        publishFeedback(false, t("models.customSaveFailed"));
      }
    } catch {
      publishFeedback(false, t("models.networkError"));
    }
  };

  const catalog = catalogState.data ?? cached;

  /*
   * Catalog loading and cold failure belong to the CATALOG PANEL, not the page.
   *
   * These used to be component-level early returns, which is correct for a page that is
   * only a catalog and wrong for a page that owns three tabs: a slow or failed catalog
   * would unmount the whole workspace, tab strip included, taking every sibling panel
   * and any unsaved combo draft with it — and on a cold failure the user could not even
   * reach Combos or Routing. Rendered below inside the catalog panel instead.
   */
  const catalogColdFailure =
    catalogState.kind === "failed-cold"
      ? catalogState.error instanceof Error
        ? catalogState.error.message
        : t("models.loadFail")
      : null;
  const catalogCold = catalogState.showSkeleton && !catalog;

  const selectedModelMap = selectedModels ?? {};

  const renderGroup = (group: ProviderModelGroup<ModelRow>) => {
    const { provider, rows, nativeProviderGroup, liveModels, discovery } = group;
    const isCollapsed = collapsed.has(provider);
    const providerDiscoveryPolicy = modelDiscovery?.providers[provider];
    const effectiveDiscoveryPolicy =
      providerDiscoveryPolicy === "on" || providerDiscoveryPolicy === "off"
        ? providerDiscoveryPolicy
        : modelDiscovery?.policy;
    // Final visibility, not just the disable flag: a model is visible to Codex only when the
    // provider allowlist admits it AND it is not disabled. Reading `disabled` alone made the
    // switches disagree with what the picker actually offers.
    const isVisible = (model: ModelRow) =>
      modelVisible(
        selectedModelMap,
        provider,
        model.id,
        model.native === true,
        disabled.has(model.namespaced),
      );
    const freeOnlyOn = freeOnly[provider] === true;
    // The control is offered only where the provider actually published per-token prices.
    // A provider whose rows are all unclassified — Ollama, a static catalog, anything with no
    // pricing in /models — would otherwise get a switch whose only possible effect is to empty
    // the list, which reads as a bug rather than as "this provider does not say".
    const pricingKnown = modelPricingKnown(rows);
    // Free-only narrows BEFORE the header counts, search, the enabled-first sort, and the PAGE
    // slice below. Filtering after the slice would leave free models stranded behind Show more
    // on a 200-row OpenRouter list, which is the exact case the issue reports.
    //
    // `scoped` is the set every count and bulk action reads. Search is deliberately NOT part of
    // it: the search box has always been a transient find-as-you-type that leaves the counts
    // alone, while Free only is a narrowing the user holds on, so a header still reading the
    // whole provider would claim more models than the list under it shows.
    // Gated on `pricingKnown` through `freeOnlyInForce`: the switch below is hidden when the
    // provider stops publishing prices, so a narrowing left on from an earlier render must lapse
    // with it rather than empty the list behind a control that is no longer there.
    const freeOnlyActive = freeOnlyInForce(freeOnlyOn, rows);
    const scoped = filterFreeModelRows(rows, freeOnlyActive);
    const activeCount = scoped.filter(isVisible).length;
    const recentForProvider = modelDiscovery?.recentArrivals[provider] ?? [];
    const recentIds = new Set(recentForProvider.map((row) => row.id));
    const capOn = contextCaps[provider] !== undefined;
    // Show the value the next enable will actually use, including a remembered selection.
    const capDisplayValue = contextCaps[provider] ?? contextCapValues[provider] ?? contextCapValue;
    // The native group offers only the three windows GPT-5.6 actually has contracts for
    // (272k live, 372k legacy, 1.05M measured); routed providers keep the generic ladder.
    // The set has to follow the list, or a saved value outside it loses its option.
    const capOptions = group.nativeProviderGroup ? NATIVE_CAP_OPTIONS : CAP_OPTIONS;
    const capOptionSet = group.nativeProviderGroup ? NATIVE_CAP_OPTION_SET : CAP_OPTION_SET;
    const discoveryFailure = liveModels && discovery?.status === "failed" ? discovery : undefined;
    const q = (search[provider] ?? "").trim().toLowerCase();
    const filtered = q ? scoped.filter((m) => m.id.toLowerCase().includes(q)) : scoped;
    // Display-only: enabled models float to the top of each provider group so they
    // stay findable in long lists. The sort is stable, so the server order is kept
    // inside each partition, and this does not affect the picker order above
    // (visibility toggles still only filter).
    const sorted = filtered.toSorted((a, b) => Number(!isVisible(a)) - Number(!isVisible(b)));
    const shown = limit[provider] ?? PAGE;
    const visible = sorted.slice(0, shown);
    const remaining = filtered.length - visible.length;
    // An empty provider has nothing to send: keep both bulk buttons inert so we never PUT an
    // empty target list (the management API rejects it with 400).
    // Bulk follows the same scoped set as the counts it sits beside: with Free only on, an
    // "All on" that enabled the 197 paid rows the header is not counting would be the exact
    // surprise the header fix exists to prevent. Pending stays keyed to the whole provider,
    // because initial discovery is a provider state that no display filter can clear.
    const hasRows = scoped.length > 0;
    const selectionPending = rows.some((model) => model.initialSelectionPending);
    const allOn = !hasRows || scoped.every(isVisible);
    const allOff = !hasRows || scoped.every((m) => !isVisible(m));
    const bulkToggle = (enable: boolean) => {
      if (!hasRows || selectionPending) return;
      void applyVisibility(
        "provider",
        provider,
        scoped.map((m) => ({ id: m.id, native: m.native === true })),
        enable,
      );
    };
    const presetControl = (() => {
      // #2465: Preset / All / Custom. Only providers with a shipped preset get the
      // control — a provider with nothing to curate would show a dead switch.
      const preset = presets[provider];
      if (!preset) return null;
      const busyHere = busy || presetBusy !== null;
      const stale =
        preset.mode === "custom" &&
        preset.appliedVersion !== undefined &&
        preset.appliedVersion < preset.availableVersion;
      return (
        <>
          <div
            className="segmented models-segmented"
            role="radiogroup"
            aria-label={t("models.presetLabel")}
          >
            {(["preset", "all"] as const).map((mode) => (
              <Button
                key={mode}
                type="button"
                role="radio"
                aria-checked={preset.mode === mode}
                size="sm"
                variant={preset.mode === mode ? "primary" : "ghost"}
                style={{
                  background: preset.mode === mode ? undefined : "transparent",
                  color: preset.mode === mode ? undefined : "var(--ocx-fg-muted)",
                }}
                disabled={busy || busyHere || selectionPending}
                onClick={(e) => {
                  e.stopPropagation();
                  // Switching from a custom selection destroys it, so consent first.
                  void applyPreset(
                    provider,
                    mode,
                    mode === "preset" && preset.mode === "custom" ? preset : undefined,
                  );
                }}
              >
                {t(`models.presetMode_${mode}` as TKey)}
              </Button>
            ))}
            {/* Custom is a STATE, not a destination: it activates on edit. Shown as a
                disabled segment so the current mode is never ambiguous. */}
            {preset.mode === "custom" && (
              <Button type="button" role="radio" aria-checked variant="primary" size="sm" disabled>
                {t("models.presetMode_custom")}
              </Button>
            )}
          </div>
          {preset.mode === "preset" && (
            <span className="muted mono text-label">
              {t("models.presetSummary", {
                count: String(preset.presetCount),
                total: String(preset.totalCount),
                version: String(preset.availableVersion),
              })}
            </span>
          )}
          {stale && (
            <span className="badge badge-amber" role="status">
              {t("models.presetUpdateAvailable", { version: String(preset.availableVersion) })}
            </span>
          )}
        </>
      );
    })();
    return (
      <div key={provider} className="card models-provider-card">
        <div className={`row group-head models-provider-head${isCollapsed ? "" : " open"}`}>
          <button
            type="button"
            className="row models-provider-toggle"
            onClick={() => toggleCollapse(provider)}
            aria-expanded={!isCollapsed}
            style={{
              flex: "1 1 auto",
              border: 0,
              background: "transparent",
              padding: 0,
              color: "inherit",
              cursor: "pointer",
              textAlign: "left",
            }}
          >
            <IconChevron
              style={{
                width: 14,
                height: 14,
                color: "var(--ocx-fg-muted)",
                transform: isCollapsed ? "none" : "rotate(90deg)",
                transition: "transform .12s",
              }}
            />
            <ProviderMark name={provider} />
            <span className="text-body font-semibold" style={{ whiteSpace: "nowrap" }}>
              {group.displayName || providerDisplaySlug(provider)}
            </span>
            {aliases.providers[provider] && (
              <span className="models-chip models-chip--identifier mono text-caption">
                {aliases.providers[provider]}
              </span>
            )}
            {nativeProviderGroup && (
              <span className="models-chip muted mono text-caption">
                {t("models.nativeGroupLabel")}
              </span>
            )}
            {discoveryFailure && (
              <span
                className="badge badge-amber"
                role="status"
                title={discoveryFailureLabel(t, discoveryFailure)}
              >
                {t("models.discoveryFailedBadge")}
              </span>
            )}
            <span className="muted mono text-label models-provider-count">
              {t("models.active", { active: activeCount, total: scoped.length })}
            </span>
            {recentForProvider.length > 0 && (
              <span className="models-chip mono text-caption">
                {t("models.newCount", { count: recentForProvider.length })}
              </span>
            )}
            {isCollapsed && capOn && (
              <span className="models-chip muted mono text-caption">
                {t("models.contextCappedValue", { value: fmtK(capDisplayValue) })}
              </span>
            )}
          </button>
          <div className="row models-provider-actions">
            {presetControl}
            <div className="models-bulk" role="group" aria-label={t("modelsV2.bulkLabel")}>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-caption"
                disabled={(busy && !visibility.pending) || allOn || selectionPending}
                onClick={() => bulkToggle(true)}
              >
                {t("models.surface.showAll")}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-caption"
                disabled={(busy && !visibility.pending) || allOff || selectionPending}
                onClick={() => bulkToggle(false)}
              >
                {t("models.surface.hideAll")}
              </Button>
            </div>
          </div>
          {/* The rare per-provider settings: a quiet second line, shown while the card is open. */}
          {!isCollapsed && (
            <details className="models-provider-settings">
              <summary>{t("models.surface.providerModelSettings")}</summary>
              <div
                className="row models-provider-actions models-provider-tools"
                role="group"
                aria-label={t("modelsV2.providerTools")}
              >
                {!nativeProviderGroup && modelDiscovery && (
                  <div className="models-discovery-policy">
                    <span className="muted text-label">
                      {t("models.surface.showNewlyDiscoveredModelsAutomatically")}
                      <small className="models-setting-description">
                        {t(
                          effectiveDiscoveryPolicy === "on"
                            ? "models.surface.newModelsShown"
                            : "models.surface.newModelsHidden",
                        )}
                      </small>
                    </span>
                    <div
                      className="segmented models-segmented"
                      role="radiogroup"
                      aria-label={t("models.surface.showNewlyDiscoveredModelsAutomatically")}
                    >
                      {(["inherit", "on", "off"] as const).map((mode) => (
                        <Button
                          key={mode}
                          type="button"
                          role="radio"
                          aria-checked={(providerDiscoveryPolicy ?? "inherit") === mode}
                          size="sm"
                          variant={
                            (providerDiscoveryPolicy ?? "inherit") === mode ? "primary" : "ghost"
                          }
                          onClick={() => void saveModelDiscovery(mode, provider)}
                        >
                          {mode === "inherit"
                            ? t("models.surface.inherit")
                            : mode === "on"
                              ? t("models.surface.show")
                              : t("models.surface.hide")}
                        </Button>
                      ))}
                    </div>
                  </div>
                )}
                <div className="models-context-setting">
                  <div>
                    <span>{t("models.surface.contextWindow")}</span>
                    <small className="models-setting-description">
                      {capOn ? (
                        <>
                          {t("models.surface.override")} {fmtK(capDisplayValue)}
                        </>
                      ) : (
                        t("models.surface.useModelSettings")
                      )}
                    </small>
                  </div>
                  <div className="models-cap-cluster">
                    {/* The label names the FUNCTION. It used to be `models.capValue` -
                  "기본 128k" - which is a value masquerading as a name: even a
                  screen-reader user was not told this governs the context window.
                  The number belongs to the adjacent Select, which is where a value
                  goes (020_control_affordances.md). */}
                    <Switch
                      on={capOn}
                      onClick={() => toggleProviderCap(provider)}
                      disabled={busy}
                      label={t("models.contextCapLabel")}
                    />
                    {/* Always rendered, disabled when the cap is off. A cap-off provider used to
                  drop this control entirely, which is the defect the user reported: openai
                  showed 1.05M and anthropic showed nothing, so the two rows started at
                  different left edges. Disabled-with-a-value is honest — it says "no
                  opinion", which is what an off cap means — and it keeps the slot occupied
                  on every card (040_cap_cluster_and_occupied_slot.md). */}
                    <>
                      <Select
                        // A saved cap outside CAP_OPTIONS is still a real selectable option
                        // (inserted below), so select it instead of falling back to "Custom";
                        // otherwise the trigger hides the persisted 128k value behind the
                        // custom-editor label.
                        value={
                          providerCapCustomOpen[provider] ? CUSTOM_OPTION : String(capDisplayValue)
                        }
                        options={[
                          ...(!capOptionSet.has(capDisplayValue) && !providerCapCustomOpen[provider]
                            ? [{ value: String(capDisplayValue), label: fmtK(capDisplayValue) }]
                            : []),
                          ...capOptions.map((v) => ({ value: String(v), label: fmtK(v) })),
                          { value: CUSTOM_OPTION, label: t("models.custom") },
                        ]}
                        onChange={(v) => onSelectProviderCap(provider, v)}
                        disabled={busy || !capOn}
                        label={t("models.capValue", { value: fmtK(capDisplayValue) })}
                        title={t("models.contextCapLabel")}
                      />
                      {/* `capOn &&` is load-bearing now that the Select no longer disappears with
                       the cap. providerCapCustomOpen is independent state: with Custom already
                       open, flipping the cap off used to leave this input and its Apply button
                       standing, and Apply sends enabled: true — turning the cap back on from a
                       field that looks like part of an off cluster. */}
                      {capOn && providerCapCustomOpen[provider] && (
                        <>
                          <input
                            className="input"
                            style={{ width: 120 }}
                            inputMode="numeric"
                            placeholder={t("models.customPlaceholder")}
                            value={providerCapCustomDraft[provider] ?? ""}
                            onChange={(e) =>
                              setProviderCapCustomDraft((prev) => ({
                                ...prev,
                                [provider]: e.target.value,
                              }))
                            }
                            onKeyDown={(e) => {
                              if (e.key === "Enter") applyProviderCustomCap(provider);
                            }}
                            disabled={busy}
                            aria-label={t("models.customPlaceholder")}
                          />
                          <Button
                            type="button"
                            onClick={() => applyProviderCustomCap(provider)}
                            disabled={busy}
                            variant="ghost"
                            size="sm"
                          >
                            {t("models.customApply")}
                          </Button>
                        </>
                      )}
                    </>
                    {/* Available on every card, including the native one: the canonical `openai` seed
                  check now admits contextWindow/modelContextWindows as user-owned overlays, and
                  the native accessors only ever narrow the measured window with them. The cap
                  beside it is the coarser sibling — one value for the whole provider.

                  It sits in the cluster for VISUAL grouping only. This is deliberately not a
                  functional merge: this button opens PER-MODEL overrides while the switch and
                  select are the provider-wide default, and two different scopes cannot honestly
                  become one control. Three separate tab stops remain. Moving it here from
                  beside the alias controls does change tab order — switch, then select when
                  enabled, then this — which reads in the order the controls are now seen. */}
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="text-caption"
                      onClick={() => openContextSettings(group)}
                      aria-haspopup="dialog"
                    >
                      {t("models.contextSettings")}
                    </Button>
                  </div>
                </div>
                <details className="models-secondary-settings">
                  <summary>{t("models.surface.aliasesAndCustomModels")}</summary>
                  <div className="models-secondary-actions">
                    {" "}
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="models-alias-edit"
                      aria-label={t("models.editProviderAlias")}
                      title={t("models.editProviderAlias")}
                      onClick={() => void saveProviderAlias(provider)}
                    >
                      <IconPencil style={{ width: 14, height: 14 }} />
                      {t("models.editProviderAlias")}
                    </Button>
                    <Switch
                      on={aliases.defaults.providers[provider] ?? aliases.defaults.global}
                      onClick={() =>
                        void setDefaultAliases(
                          !(aliases.defaults.providers[provider] ?? aliases.defaults.global),
                          provider,
                        )
                      }
                      label={t("models.useDefaultAliases")}
                      showLabel
                    />
                    {
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="text-caption"
                        onClick={(e) => {
                          e.stopPropagation();
                          setCustomModalProvider(provider);
                          setCustomModalOpen(true);
                        }}
                        aria-haspopup="dialog"
                      >
                        <span aria-hidden="true">+</span> {t("models.customAdd")}
                      </Button>
                    }
                  </div>
                </details>
              </div>
            </details>
          )}
        </div>
        {!isCollapsed && (
          <div className="models-provider-body">
            <ModelDiscoveryStatus
              key={provider}
              discovery={discovery}
              apiBase={liveModels ? apiBase : undefined}
              provider={provider}
              onRefreshed={() => {
                void load(true);
              }}
            />
            {nativeProviderGroup && (
              <p className="muted text-label models-provider-hint">{t("models.nativeHint")}</p>
            )}
            {rows.length === 0 && (
              <EmptyProviderHint
                liveModels={liveModels}
                discovery={discovery}
                showFailureBadge={false}
              />
            )}
            {/* A group WITH rows and a failed fetch got the amber header badge and nothing that
                explains the dependency; #4075 is the reporter having to discover on their own
                that turning discovery off is what makes a manually added model usable. */}
            {rows.length > 0 && discoveryFailure && <DiscoveryDependencyHint />}
            {/* One filter bar over the list: find, narrow to free rows, and how new rows arrive. */}
            {(rows.length > PAGE / 2 || pricingKnown) && (
              <div className="row models-provider-filters">
                {rows.length > PAGE / 2 && (
                  <input
                    className="input"
                    placeholder={t("models.search")}
                    value={search[provider] ?? ""}
                    onChange={(e) => setSearch((prev) => ({ ...prev, [provider]: e.target.value }))}
                    aria-label={t("models.search")}
                  />
                )}
                {pricingKnown && (
                  <Switch
                    on={freeOnlyOn}
                    onClick={() => setFreeOnly((prev) => ({ ...prev, [provider]: !freeOnlyOn }))}
                    label={t("models.freeOnly")}
                    showLabel
                  />
                )}
              </div>
            )}

            {/* Reads `scoped`, not `filtered`: with a search term that matches nothing, the
                honest message is the search one, not "this provider has no free models". */}
            {freeOnlyActive && scoped.length === 0 && rows.length > 0 && (
              <p className="muted text-label" role="status">
                {t("models.noFreeMatch")}
              </p>
            )}
            {visible.map((model) => (
              <CatalogModelRow
                key={model.namespaced}
                model={model}
                selected={isVisible(model)}
                alias={aliases.models[provider]?.[model.id]}
                recent={recentIds.has(model.id)}
                contextCapValue={contextCapValue}
                disabled={busy && !visibility.pending}
                onToggle={() =>
                  void applyVisibility(
                    "models",
                    provider,
                    [{ id: model.id, native: model.native === true }],
                    !isVisible(model),
                  )
                }
                onEdit={() => setSettingsModel(model)}
                onDelete={deleteCustomModel}
              />
            ))}
            {remaining > 0 && (
              <Button
                type="button"
                onClick={() => setLimit((prev) => ({ ...prev, [provider]: shown + PAGE }))}
                variant="ghost"
                size="sm"
                className="models-show-more"
              >
                {t("models.showMore", { n: remaining })}
              </Button>
            )}
          </div>
        )}
      </div>
    );
  };

  const visibleGroups = selectedProvider
    ? groups.filter((group) => group.provider === selectedProvider)
    : groups;

  const controlsBlock = (
    <>
      <div className="models-control-top-row">
        {modelDiscovery && (
          <SettingsRow label={t("models.newPolicyGlobal")}>
            <Switch
              on={modelDiscovery.policy === "off"}
              onClick={() =>
                void saveModelDiscovery(modelDiscovery.policy === "off" ? "on" : "off")
              }
              label={t("models.newPolicyGlobal")}
            />
          </SettingsRow>
        )}
        <SettingsRow label={t("models.useDefaultAliasesGlobal")}>
          <Switch
            on={aliases.defaults.global}
            onClick={() => void setDefaultAliases(!aliases.defaults.global)}
            label={t("models.useDefaultAliasesGlobal")}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setShowAliases((value) => !value)}
          >
            {t("models.aliases")}
          </Button>
        </SettingsRow>
        {null}
      </div>

      <div className="row models-cap-row">
        <span className="muted text-control">{t("models.contextCapLabel")}</span>
        <Select
          value={showCustom ? CUSTOM_OPTION : String(contextCapValue)}
          options={[
            ...(!CAP_OPTION_SET.has(contextCapValue) && !showCustom
              ? [{ value: String(contextCapValue), label: fmtK(contextCapValue) }]
              : []),
            ...CAP_OPTIONS.map((v) => ({ value: String(v), label: fmtK(v) })),
            { value: CUSTOM_OPTION, label: t("models.custom") },
          ]}
          onChange={(v) => onSelectCap(v)}
          disabled={busy}
          label={t("models.contextCapLabel")}
        />
        {showCustom && (
          <>
            <input
              className="input"
              style={{ width: 160 }}
              inputMode="numeric"
              placeholder={t("models.customPlaceholder")}
              value={customCap}
              onChange={(e) => setCustomCap(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyCustomCap();
              }}
              disabled={busy}
              aria-label={t("models.customPlaceholder")}
            />
            <Button
              type="button"
              onClick={applyCustomCap}
              disabled={busy}
              variant="ghost"
              size="sm"
            >
              {t("models.customApply")}
            </Button>
          </>
        )}
        <Switch on={allCapped} onClick={setAll} disabled={busy} label={t("models.setAll")} />
        <HelpPopover label={t("models.contextCapLabel")}>
          {t("models.setAllHint", { value: fmtK(contextCapValue) })}
        </HelpPopover>
      </div>

      {null}
      {showAliases && (
        <div className="card models-aliases-card" aria-label={t("models.aliasesTable")}>
          <div className="row group-head">
            <strong>{t("models.aliases")}</strong>
          </div>
          {Object.entries(aliases.models).flatMap(([provider, rows]) =>
            Object.entries(rows).map(([model, value]) => (
              <div className="row models-model-row" key={`${provider}/${model}`}>
                <code className="mono text-caption" style={{ flex: 1 }}>
                  {provider}/{model}
                </code>
                <strong className="mono text-control">{value.alias}</strong>
                <span className="models-chip muted text-caption">
                  {value.source === "builtin" ? t("models.aliasAuto") : t("models.aliasUser")}
                </span>
                {value.stale && <span className="badge badge-amber">{t("models.aliasStale")}</span>}
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={t("models.editModelAlias")}
                  onClick={() => void saveModelAlias(provider, model)}
                >
                  <IconPencil style={{ width: 13, height: 13 }} />
                </Button>
              </div>
            )),
          )}
        </div>
      )}
    </>
  );

  const customCount = models.filter((m) => m.custom).length;
  const collapseControls = (
    <div className="row models-collapse-controls">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="text-caption"
        onClick={() => setAllCollapsed(true)}
        disabled={busy}
      >
        <IconChevron width={12} height={12} aria-hidden="true" /> {t("models.collapseAll")}
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="text-caption"
        onClick={() => setAllCollapsed(false)}
        disabled={busy}
      >
        <IconChevron
          width={12}
          height={12}
          aria-hidden="true"
          style={{ transform: "rotate(90deg)" }}
        />{" "}
        {t("models.expandAll")}
      </Button>
      <HelpPopover label={t("nav.models")}>{t("models.orderHint")}</HelpPopover>
      <CustomModelsSummary
        count={customCount}
        label={t("models.customSummary", { count: customCount })}
      />
    </div>
  );

  const emptyStateBlock = (
    <>
      {groups.length === 0 && (
        <EmptyState icon={<IconBoxes />} title={t("models.noRouted")}>
          {t("models.noRoutedHint")}
        </EmptyState>
      )}
    </>
  );

  const modalsBlock = (
    <>
      {contextGroup && (
        <ContextSettingsDialog
          apiBase={apiBase}
          group={contextGroup}
          onClose={() => setContextGroup(null)}
          onSaved={async (changed, application) => {
            reportClientApplication(application);
            publishFeedback(
              true,
              application?.status === "pending"
                ? c("pendingApply")
                : t(changed ? "models.contextSaved" : "models.contextUnchanged"),
            );
            if (changed) await load(true);
          }}
        />
      )}

      {customModalOpen && (
        <CustomModelDialog
          apiBase={apiBase}
          provider={customModalProvider}
          models={models}
          onClose={() => setCustomModalOpen(false)}
          onSaved={async (application) => {
            reportClientApplication(application);
            publishFeedback(true, t("models.customAdded"));
            await load(true);
          }}
        />
      )}
    </>
  );

  /*
   * The catalog tab body: everything this page rendered before it grew tabs. It keeps
   * `.models-workspace-shell`, so the wider-column rule and every workspace style below
   * it apply unchanged.
   */
  const catalogPanel = (
    <div className="models-workspace-shell">
      {status && (
        <div
          className={`action-toast notice ${ok ? "notice-ok" : "notice-err"}`}
          role="status"
          aria-live="polite"
        >
          {ok ? <IconCheck /> : <IconAlert />}
          <span>{status}</span>
        </div>
      )}
      {/* Keep the last-good catalog interactive but make a failed revalidation explicit. */}
      {catalogState.showError && <Notice tone="err">{t("models.loadFail")}</Notice>}
      {modelDiscovery?.ambiguousDisabledSelectors?.length ? (
        <Notice tone="warn">
          {t("models.ambiguousDisabledSelectors", {
            selectors: modelDiscovery.ambiguousDisabledSelectors.join(", "),
          })}
        </Notice>
      ) : null}
      {integrationWarning && (
        <div className="models-integration-warning">
          <Notice tone="warn">{integrationWarning}</Notice>
        </div>
      )}
      <ModelsSettingsPanel
        title={t("models.surface.globalModelDefaults")}
        attentionLabel={t("models.settingsPanel.attention")}
        warn={false}
        summary={modelsSettingsSummary(t, {
          windowOn: allCapped,
          windowValue: contextCapValue,
          newModelsOff: modelDiscovery?.policy === "off",
          aliasesOn: aliases.defaults.global,
        })}
      >
        {controlsBlock}
      </ModelsSettingsPanel>
      <div
        className="models-workspace-root"
        aria-busy={visibility.pending || catalogState.refreshing || undefined}
      >
        <ModelsProviderRail
          selected={selectedProvider}
          onSelect={setSelectedProvider}
          all={{ active: effectiveVisibleCount, total: models.length }}
          // Same final-visibility rule as the provider card, so the rail never disagrees with it.
          items={groups.map(({ provider, rows }) => ({
            provider,
            total: rows.length,
            active: rows.filter((m) =>
              modelVisible(
                selectedModelMap,
                provider,
                m.id,
                m.native === true,
                disabled.has(m.namespaced),
              ),
            ).length,
          }))}
        />
        <section className="models-workspace-main" aria-label={t("models.workspace.mainAria")}>
          {collapseControls}
          <div className="models-provider-list">
            {visibleGroups.map((group) => renderGroup(group))}
          </div>
          {groups.length === 0 && emptyStateBlock}
        </section>
      </div>
      {modalsBlock}
    </div>
  );

  return (
    <>
      {/*
        Title, one plain line for the active tab, and the one page action. The long
        description and the catalog's delivery process open from "?", rendered here rather
        than in the panel because hidden panels stay mounted.
      */}
      <ModelsReloadFocusBoundary
        stale={appServerState === "stale"}
        headRef={reloadHeadRef}
        bannerRef={reloadBannerRef}
      >
        <PageHeader
          title={t("nav.models")}
          subtitle={t("models.surface.description")}
          actions={
            <>
              {SUBTITLE_TKEY[tab][1] && (
                <ModelsAbout key={tab} text={t(SUBTITLE_TKEY[tab][1])}>
                  {tab === "catalog" && <p>{t("models.delivery.hint")}</p>}
                </ModelsAbout>
              )}
              {appServerState !== "stale" && (
                <Button
                  data-models-reload-action
                  ref={reloadHeadRef}
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    void handleCodexRestart();
                  }}
                  disabled={codexRestarting}
                >
                  <span className={codexRestarting ? "inline-flex animate-spin" : "inline-flex"}>
                    <IconRefresh aria-hidden="true" />
                  </span>
                  {codexRestarting ? t("dash.codexRestarting") : t("dash.codexRestart")}
                </Button>
              )}
            </>
          }
        />
        <CodexStaleBanner
          state={appServerState}
          actionRef={reloadBannerRef}
          controller={{ restarting: codexRestarting, restart: handleCodexRestart }}
        />
      </ModelsReloadFocusBoundary>
      <ModelsTabStrip tab={tab} onSelect={selectTab} meta={tabMeta} />

      {/*
        Panels mount lazily and then stay mounted, hidden — a half-typed combo draft
        survives a tab hop. `hidden` matches the APG examples and the existing Logs tab.
        Each panel owns an error boundary so one failing tab cannot take the others with
        it; App's page-level boundary is keyed by page and would otherwise stay tripped
        across a tab switch.
      */}
      <div
        className="models-tab-panel"
        role="tabpanel"
        id={modelsPanelDomId("catalog")}
        aria-labelledby={modelsTabDomId("catalog")}
        hidden={tab !== "catalog"}
      >
        <ErrorBoundary
          pageName={t("models.tab.catalog")}
          title={t("errorBoundary.title")}
          message={t("errorBoundary.message")}
          detailsLabel={t("errorBoundary.details")}
          reloadLabel={t("errorBoundary.reload")}
        >
          {catalogCold ? (
            <DataSurfaceSkeleton label={t("models.loading")} rows={5} />
          ) : catalogColdFailure !== null ? (
            <>
              <Notice tone="err">{catalogColdFailure}</Notice>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => catalogResource.refresh()}
              >
                {t("common.retry")}
              </Button>
            </>
          ) : (
            catalogPanel
          )}
        </ErrorBoundary>
      </div>

      {/*
        The panel SHELL is always present; only its contents mount lazily. A conditional
        wrapper left the tab's `aria-controls` pointing at an element that did not exist
        until the tab had been visited once.
      */}

      <div
        className="models-tab-panel"
        role="tabpanel"
        id={modelsPanelDomId("routing")}
        aria-labelledby={modelsTabDomId("routing")}
        hidden={tab !== "routing"}
      >
        {mounted.has("routing") && (
          <ErrorBoundary
            pageName={t("models.tab.routing")}
            title={t("errorBoundary.title")}
            message={t("errorBoundary.message")}
            detailsLabel={t("errorBoundary.details")}
            reloadLabel={t("errorBoundary.reload")}
          >
            <>
              <PlannedFeature
                title={t("models.surface.advancedRouting")}
                description={t("models.surface.routingDescription")}
              />
            </>
          </ErrorBoundary>
        )}
      </div>

      {settingsModel && (
        <ModelSettingsDialog
          key={`${apiBase}/${settingsModel.namespaced}`}
          row={settingsModel}
          apiBase={apiBase}
          onRefresh={(signal) => load(true, signal)}
          onFeedback={publishFeedback}
          onClose={() => setSettingsModel(null)}
        />
      )}
    </>
  );
}
