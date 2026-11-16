import { createBoundedFetch, type BoundedFetch } from "@/bounded-fetch";
import { fetchCodexAppServerState, type AppServerStateOutcome } from "@/codex-app-server-state";
import type { NoticeTone } from "@/ui";
import { useCodexRestart } from "@/use-codex-restart";
import { useCallback, useEffect, useRef, useState } from "react";
/** Observes Codex catalog application; independent of directory and model edits. */
export function useCatalogClientStatus(
  apiBase: string,
  restartEpoch: number,
  reportRestart: (message: string, tone: NoticeTone) => void,
) {
  // Codex app-server staleness (devlog/_fin/260815_gui_codex_restart). Named
  // appServerState, not catalogState: this file already binds that name to the
  // model-catalog resource state, which is an unrelated concept. (Spelling the
  // catalog route here would register a phantom endpoint with the CLI parity
  // sweep, which reads GUI sources for api paths.)
  const [appServerState, setAppServerState] = useState<AppServerStateOutcome["state"]>(null);
  const reloadHeadRef = useRef<HTMLButtonElement>(null),
    reloadBannerRef = useRef<HTMLButtonElement>(null);
  // A restart request outlives a navigation away from this page, so its completion
  // callback must not set state after unmount.
  const appServerMounted = useRef(true);
  useEffect(() => {
    appServerMounted.current = true;
    return () => {
      appServerMounted.current = false;
    };
  }, []);

  const appServerRead = useRef<BoundedFetch | null>(null);
  const appServerReadGeneration = useRef(0);
  const appServerReadBase = useRef(apiBase);
  const cancelAppServerRead = useCallback(() => {
    appServerReadGeneration.current++;
    appServerRead.current?.controller.abort();
    appServerRead.current?.clear();
    appServerRead.current = null;
  }, []);
  const reloadAppServerState = useCallback(async () => {
    // An old restart callback must not start an A read after the page moved to B.
    if (!appServerMounted.current || appServerReadBase.current !== apiBase) return;
    cancelAppServerRead();
    const generation = appServerReadGeneration.current;
    const bounded = createBoundedFetch(15_000);
    appServerRead.current = bounded;
    try {
      const outcome = await fetchCodexAppServerState(apiBase, { signal: bounded.signal });
      if (
        bounded.signal.aborted ||
        !appServerMounted.current ||
        appServerReadBase.current !== apiBase ||
        generation !== appServerReadGeneration.current ||
        appServerRead.current !== bounded
      )
        return;
      setAppServerState(outcome.state);
    } finally {
      // The observation owns its deadline until settlement, independently of PUT.
      bounded.clear();
      if (appServerRead.current === bounded) appServerRead.current = null;
    }
  }, [apiBase, cancelAppServerRead]);

  // onSettled, not a per-button callback: the sidebar control knows nothing about
  // this page, and a restart succeeding there must still clear the banner here.
  const { restarting: codexRestarting, restart: handleCodexRestart } = useCodexRestart(apiBase, {
    onSettled: () => {
      void reloadAppServerState();
    },
    restoreFocusFallback: (previous) =>
      previous?.hasAttribute("data-models-reload-action")
        ? (reloadBannerRef.current ?? reloadHeadRef.current)
        : null,
    // Reported through the shell, not this page's toast: a restart takes up to 30s and
    // outlives a navigation away, and an outcome that says app-servers are still running
    // must not be discarded because the user moved on while waiting for it.
    report: reportRestart,
  });

  useEffect(() => {
    // Once on mount/base change or restart completion, never on a timer.
    appServerReadBase.current = apiBase;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Clear the previous server snapshot before starting the read for this base and restart.
    setAppServerState(null);
    void reloadAppServerState();
    return cancelAppServerRead;
  }, [apiBase, cancelAppServerRead, reloadAppServerState, restartEpoch]);
  return { appServerState, reloadHeadRef, reloadBannerRef, codexRestarting, handleCodexRestart };
}
