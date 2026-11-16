import { useSyncExternalStore } from "react";
import { USAGE_SESSION_CHANGED, usageSessionSnapshot } from "../../api";

function subscribe(listener: () => void) {
  window.addEventListener(USAGE_SESSION_CHANGED, listener);
  return () => window.removeEventListener(USAGE_SESSION_CHANGED, listener);
}

export function useUsageSession() {
  return useSyncExternalStore(subscribe, usageSessionSnapshot, usageSessionSnapshot);
}
