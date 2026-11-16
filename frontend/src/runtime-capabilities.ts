import { createContext, useContext } from "react";
import type { Page } from "./app-routing";

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
export const useRuntimeCapabilities = () => useContext(RuntimeCapabilitiesContext)!;
