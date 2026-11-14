import { createContext, useContext } from "react";
import type { Page } from "./app-routing";
import { useI18n } from "./i18n/shared";

export interface RuntimeCapabilities {
  runtime: string;
  pages: Page[];
  adapters: string[];
  clients: string[];
  oauth: boolean;
  oauthProviders?: string[];
  usage?: boolean;
  quota?: boolean;
  debugLogs?: boolean;
}

export const RuntimeCapabilitiesContext = createContext<RuntimeCapabilities | null>(null);
export const useRuntimeCapabilities = () => useContext(RuntimeCapabilitiesContext);

export function useMigrationLabel(): string {
  const { locale } = useI18n();
  return locale === "zh" || locale === "zh-TW" ? "尚未迁移" : "Not migrated yet";
}
