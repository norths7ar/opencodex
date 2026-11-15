import { useState, type ReactNode } from "react";
import { ProviderSessionContext, type ProviderLoginStatuses } from "./provider-session-context";

/** Login observations belong to the gateway session, not an individual page visit. */
export function ProviderSession({ children }: { children: ReactNode }) {
  const state = useState<ProviderLoginStatuses>({});
  return (
    <ProviderSessionContext.Provider value={state}>{children}</ProviderSessionContext.Provider>
  );
}
