import { createContext, type Dispatch, type SetStateAction } from "react";
import type { OAuthStatus } from "../pages/providers-shared";

export type ProviderLoginStatuses = Record<string, OAuthStatus>;
export const ProviderSessionContext = createContext<
  [ProviderLoginStatuses, Dispatch<SetStateAction<ProviderLoginStatuses>>] | null
>(null);
