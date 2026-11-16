import { useDataSurface } from "../data-surface";
import { useRef } from "react";
import { readJsonOrThrow } from "../fetch-json";

export interface StartupHealth {
  serviceOperation: { id: string; state: string; error?: string | null } | null;
  taskName: string;
  stateDirectory: string;
  runtimeState: string;
  logPath: string;
  supervisorLogPath: string;
  serviceInstalled: boolean;
  serviceEnabled: boolean;
  serviceViable: boolean;
  serviceStale: boolean;
  serviceConflict: boolean;
  diagnosticStale: boolean;
  error: string | null;
  commands: Record<string, string>;
}

export interface Overview {
  runtime: { version: string; uptimeSeconds: number; port: number; status: string };
  startup: StartupHealth;
  channels: { total: number; enabled: number };
  milestones: { first_success_at: string | null };
}

export function useOverview(apiBase: string) {
  const forceDiagnostics = useRef(false);
  // eslint-disable-next-line local-i18n/no-hardcoded-ui-strings -- Internal resource identity, never rendered.
  const resourceKey = `overview:${apiBase}`;
  const overview = useDataSurface(
    resourceKey,
    [apiBase],
    async (signal) => {
      const path = forceDiagnostics.current ? "/api/overview?force=true" : "/api/overview";
      forceDiagnostics.current = false;
      const response = await fetch(apiBase + path, { signal });
      const value = await readJsonOrThrow<Overview>(response);
      if (!value) throw new Error("Missing overview response.");
      return value;
    },
    { isEmpty: () => false, pollMs: 30_000 },
  );
  return {
    ...overview,
    refreshDiagnostics: () => {
      forceDiagnostics.current = true;
      overview.refresh();
    },
  };
}
