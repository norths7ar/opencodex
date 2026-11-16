import { useDataSurface } from "@/data-surface";
import { readJsonOrThrow } from "@/fetch-json";
import { useState } from "react";

export type DelegationModelOption = { provider: string; model: string; namespaced: string };
export type DelegationPatch = {
  syncCodexSubagentDefaults?: boolean;
  model?: string | null;
  effort?: string | null;
};
type NativeSettings = DelegationPatch & { efforts: string[]; available: DelegationModelOption[] };

export function useSubagentDelegation(apiBase: string) {
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const resource = useDataSurface(
    `subagents-native:${apiBase}`,
    [apiBase],
    (signal) =>
      fetch(`${apiBase}/api/subagent-defaults`, { signal }).then(readJsonOrThrow<NativeSettings>),
    { isEmpty: () => false },
  );
  const save = async (patch: DelegationPatch) => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await fetch(`${apiBase}/api/subagent-defaults`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }).then(readJsonOrThrow);
      resource.refresh();
    } catch (cause) {
      setSaveError(String(cause));
    } finally {
      setSaving(false);
    }
  };
  return {
    saving,
    model: resource.data?.model ?? "",
    effort: resource.data?.effort ?? "",
    efforts: resource.data?.efforts ?? [],
    available: resource.data?.available ?? [],
    syncCodexDefaults: resource.data?.syncCodexSubagentDefaults === true,
    save,
    error: saveError ?? (resource.state.showError ? String(resource.state.error) : null),
    refresh: resource.refresh,
    loading: resource.state.showSkeleton,
  };
}
