import { useDataSurface } from "@/data-surface";
import { clientApplicationWarning } from "@/contracts/application";
import type { ApplicationReceipt } from "@/contracts/channels";
import { readChannelViews } from "@/features/channels/api";
import { readJsonOrThrow } from "@/fetch-json";
import { useI18n } from "@/i18n/shared";
import {
  sidecarPatchForSave,
  visionModelOptions,
  webSearchModelOptionsForPicker,
  type ModelInfo,
  type SettingsData,
  type SidecarData,
  type SidecarPatch,
} from "@/pages/dashboard-shared";
import { useState } from "react";

export function useGatewaySettings(apiBase: string) {
  const { t, locale } = useI18n();
  const [sidecarSaving, setSidecarSaving] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applicationWarning, setApplicationWarning] = useState<string>();
  const reportClientApplication = (application?: ApplicationReceipt) => {
    if (application) {
      setApplicationWarning(
        clientApplicationWarning(application, t("integrations.catalogSyncFailed")),
      );
    }
  };
  const sidecarRead = useDataSurface(
    `gateway-sidecars:${apiBase}`,
    [apiBase],
    (signal) =>
      fetch(`${apiBase}/api/sidecar-settings`, { signal }).then(readJsonOrThrow<SidecarData>),
    { isEmpty: () => false },
  );
  const settingsRead = useDataSurface(
    `gateway-settings:${apiBase}`,
    [apiBase],
    (signal) => fetch(`${apiBase}/api/settings`, { signal }).then(readJsonOrThrow<SettingsData>),
    { isEmpty: () => false },
  );
  const modelsRead = useDataSurface(
    `gateway-models:${apiBase}`,
    [apiBase],
    (signal) => fetch(`${apiBase}/api/models`, { signal }).then(readJsonOrThrow<ModelInfo[]>),
    { isEmpty: (rows) => !rows?.length },
  );
  const channelRead = useDataSurface(
    `gateway-channels:${apiBase}`,
    [apiBase],
    (signal) => readChannelViews(apiBase, signal),
    { isEmpty: (rows) => !rows.length },
  );
  const sidecar = sidecarRead.data;
  const models = modelsRead.data ?? [];
  const saveSidecar = async (patch: SidecarPatch) => {
    if (!sidecar || sidecarSaving) return;
    setSidecarSaving(true);
    setError(null);
    try {
      const result = await fetch(`${apiBase}/api/sidecar-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sidecarPatchForSave(sidecar, patch)),
      }).then(readJsonOrThrow<SidecarData & { catalogRefresh: ApplicationReceipt }>);
      reportClientApplication(result?.catalogRefresh);
      sidecarRead.refresh();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSidecarSaving(false);
    }
  };
  const toggleCompaction = async () => {
    if (!settingsRead.data || settingsSaving) return;
    setSettingsSaving(true);
    setError(null);
    try {
      const result = await fetch(`${apiBase}/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ codexClientCompaction: !settingsRead.data.codexClientCompaction }),
      }).then(readJsonOrThrow<{ catalogRefresh: ApplicationReceipt }>);
      reportClientApplication(result?.catalogRefresh);
      settingsRead.refresh();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSettingsSaving(false);
    }
  };
  const refresh = () => {
    sidecarRead.refresh();
    settingsRead.refresh();
    modelsRead.refresh();
    channelRead.refresh();
  };
  return {
    apiBase,
    t,
    locale,
    sidecar,
    sidecarSaving,
    sidecarLoading: sidecarRead.state.showSkeleton,
    applicationWarning,
    saveSidecar,
    models,
    providers: (channelRead.data ?? []).map((value) => ({
      ...value,
      hasApiKey: value.hasApiKey === true,
    })),
    sidecarModels: webSearchModelOptionsForPicker(
      sidecar?.webSearchModels,
      models,
      sidecar?.webSearch?.model,
      sidecar?.webSearch?.backend === "routed" ? undefined : sidecar?.webSearch?.backend,
    ),
    visionModels: visionModelOptions(
      sidecar?.visionModels,
      models,
      sidecar?.vision?.model,
      sidecar?.vision?.backend,
    ),
    settings: settingsRead.data,
    settingsSaving,
    toggleCompaction,
    refresh,
    error:
      error ??
      ([sidecarRead, settingsRead, modelsRead, channelRead].some(
        (resource) => resource.state.showError,
      )
        ? t("dash.dataUnavailable")
        : null),
  };
}
