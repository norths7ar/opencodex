/**
 * State behind the Models settings panel: its persisted open flag and the summary line
 * that names every folded control. Kept apart from the component file so Fast Refresh
 * sees a components-only module there.
 */
import type { TFn } from "../i18n/shared";
import type { StorageLike } from "./collapse-store";
import { fmtK } from "./models-shared";

export const SETTINGS_OPEN_KEY = "ocx.models.settingsOpen.v1";

export interface SettingsSummaryItem {
  id: string;
  label: string;
  value: string;
}

function resolveStorage(storage?: StorageLike): StorageLike | undefined {
  if (storage) return storage;
  return typeof localStorage === "undefined" ? undefined : localStorage;
}

export function readSettingsOpen(storage?: StorageLike): boolean {
  try {
    return resolveStorage(storage)?.getItem(SETTINGS_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeSettingsOpen(open: boolean, storage?: StorageLike): void {
  try {
    resolveStorage(storage)?.setItem(SETTINGS_OPEN_KEY, open ? "1" : "0");
  } catch {
    /* quota / private mode: the fold is a preference, never a failure */
  }
}

export interface ModelsSettingsState {
  windowOn: boolean;
  windowValue: number;
  newModelsOff: boolean;
  aliasesOn: boolean;
}
export function modelsSettingsSummary(t: TFn, state: ModelsSettingsState): SettingsSummaryItem[] {
  const items: SettingsSummaryItem[] = [
    {
      id: "window",
      label: t("models.contextCapLabel"),
      value: state.windowOn ? fmtK(state.windowValue) : t("models.settingsPanel.off"),
    },
  ];
  if (state.newModelsOff)
    items.push({ id: "new-models", label: t("models.newPolicyGlobal"), value: "" });
  if (state.aliasesOn)
    items.push({ id: "aliases", label: t("models.useDefaultAliasesGlobal"), value: "" });
  return items;
}
