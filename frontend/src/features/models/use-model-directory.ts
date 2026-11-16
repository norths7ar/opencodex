import { setClientResourceData } from "@/client-resource";
import { useDataSurface } from "@/data-surface";
import { readChannelViews } from "@/features/channels/api";
import { readJsonOrThrow } from "@/fetch-json";
import {
  fetchSelectedModels,
  shouldApplyLoadGeneration,
  type ProviderModelMap,
} from "@/model-visibility";
import type { ConfiguredProviderSummary } from "@/models-groups";
import {
  collectDisabledNamespaced,
  type ModelRow,
  type ProviderContextCapsResponse,
} from "@/pages/models-shared";
import { readSessionListCache, writeSessionListCache } from "@/session-list-cache";
import { useCallback, useMemo, useRef, useState } from "react";
type CachedModelsPage = {
  models: ModelRow[];
  providers: ConfiguredProviderSummary[];
  selectedModels: ProviderModelMap;
  disabled: string[];
  contextCaps: Record<string, number>;
  contextCapValues?: Record<string, number>;
  contextCapValue: number;
};

/** Owns local catalog snapshots and serializes their application against mutations. Upstream discovery is a separate explicit command. */
export function useModelDirectory(apiBase: string, catalogActive: boolean) {
  const cacheKey = `ocx.models.catalog.v1:${apiBase}`;
  const cached = useMemo(() => readSessionListCache<CachedModelsPage>(cacheKey), [cacheKey]);
  const [models, setModels] = useState<ModelRow[]>(() => cached?.models ?? []);
  const [providers, setProviders] = useState<ConfiguredProviderSummary[]>(
    () => cached?.providers ?? [],
  );
  const [disabled, setDisabled] = useState<Set<string>>(() => new Set(cached?.disabled ?? []));
  const [selectedModels, setSelectedModels] = useState<ProviderModelMap | null>(
    () => cached?.selectedModels ?? null,
  );
  const [contextCaps, setContextCaps] = useState<Record<string, number>>(
    () => cached?.contextCaps ?? {},
  );
  const [contextCapValues, setContextCapValues] = useState<Record<string, number>>(
    () => cached?.contextCapValues ?? {},
  );
  const [contextCapValue, setContextCapValue] = useState(() => cached?.contextCapValue ?? 350_000);
  const catalogMutationRef = useRef(false);
  const loadGenerationRef = useRef(0);
  const loadPendingRef = useRef(false);
  const fetchCatalog = useCallback(
    async (signal: AbortSignal): Promise<CachedModelsPage> => {
      const generation = loadGenerationRef.current;
      const [data, capsData, selectionData, providerData] = await Promise.all([
        // Every request carries the resource signal, so leaving the catalog tab cancels
        // the work rather than only discarding its result.
        fetch(`${apiBase}/api/models`, { signal }).then(readJsonOrThrow<ModelRow[]>),
        fetch(`${apiBase}/api/provider-context-caps`, { signal }).then(
          readJsonOrThrow<ProviderContextCapsResponse>,
        ),
        fetchSelectedModels(apiBase, fetch, signal),
        readChannelViews(apiBase, signal),
      ]);
      if (data === undefined || capsData === undefined || providerData === undefined) {
        throw new Error("models payload missing");
      }
      if (signal.aborted) throw new Error("models request aborted");
      const nextDisabled = collectDisabledNamespaced(data);
      const value =
        typeof capsData.value === "number" && Number.isFinite(capsData.value) && capsData.value > 0
          ? capsData.value
          : typeof capsData.cap === "number" && Number.isFinite(capsData.cap) && capsData.cap > 0
            ? capsData.cap
            : undefined;
      const nextCapValue = value !== undefined ? value : 350_000;
      const next = {
        models: data,
        providers: providerData,
        selectedModels: selectionData,
        disabled: [...nextDisabled],
        contextCaps: capsData.caps ?? {},
        contextCapValues: capsData.values ?? capsData.caps ?? {},
        contextCapValue: nextCapValue,
      } satisfies CachedModelsPage;
      if (generation === loadGenerationRef.current) writeSessionListCache(cacheKey, next);
      return next;
    },
    [apiBase, cacheKey],
  );

  const applyCatalog = useCallback((next: CachedModelsPage) => {
    setModels(next.models);
    setProviders(next.providers);
    setDisabled(new Set(next.disabled));
    setSelectedModels(next.selectedModels);
    setContextCapValue(next.contextCapValue);
    setContextCaps(next.contextCaps);
    setContextCapValues(next.contextCapValues ?? next.contextCaps);
  }, []);

  const catalogResource = useDataSurface<CachedModelsPage>(
    cacheKey,
    [apiBase],
    async (signal) => {
      const generation = loadGenerationRef.current;
      const next = await fetchCatalog(signal);
      // A manual mutation refresh may have invalidated this request while its JSON was decoding.
      // Do not let the aborted catalog repaint controls after the newer result is applied.
      if (signal.aborted) throw new Error("models request aborted");
      if (!catalogMutationRef.current && generation === loadGenerationRef.current)
        applyCatalog(next);
      return next;
    },
    // Poll the local snapshot only while the catalog tab is visible.
    {
      isEmpty: () => false,
      pollMs: 10_000,
      initialData: cached ?? undefined,
      enabled: catalogActive,
    },
  );
  const catalogState = catalogResource.state;

  const load = useCallback(
    async (force = false, signal?: AbortSignal): Promise<boolean> => {
      if (loadPendingRef.current && !force) return false;
      loadPendingRef.current = true;
      const generation = ++loadGenerationRef.current;
      try {
        const next = await fetchCatalog(signal ?? new AbortController().signal);
        if (!shouldApplyLoadGeneration(generation, loadGenerationRef.current)) return false;
        applyCatalog(next);
        // Follow-up mutation refreshes retain their existing awaitable contract while publishing
        // the result through the same shared store used by the initial catalog subscription.
        setClientResourceData(cacheKey, next);
        return true;
      } catch {
        return false;
      } finally {
        if (shouldApplyLoadGeneration(generation, loadGenerationRef.current)) {
          loadPendingRef.current = false;
        }
      }
    },
    [applyCatalog, cacheKey, fetchCatalog],
  );

  const invalidate = () => {
    ++loadGenerationRef.current;
  };
  const setMutation = (busy: boolean) => {
    invalidate();
    loadPendingRef.current = false;
    catalogMutationRef.current = busy;
  };
  const beginMutation = () => {
    if (catalogMutationRef.current) return false;
    setMutation(true);
    return true;
  };
  return {
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
    invalidate,
    setMutation,
    beginMutation,
  };
}
