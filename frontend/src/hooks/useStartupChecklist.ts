import { useKeyedClientResource } from "../client-resource";
import { startupChecklistDone } from "../startup-health-ui";

type StartupBody = Parameters<typeof startupChecklistDone>[0];

export function useStartupChecklist(apiBase: string) {
  const key = `home-startup:${apiBase}`;
  const load = async (signal: AbortSignal): Promise<StartupBody> => {
    const response = await fetch(`${apiBase}/api/startup-health`, { signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body: unknown = await response.json();
    if (!body || typeof body !== "object" || Array.isArray(body))
      throw new Error("Invalid startup health response");
    return body as StartupBody;
  };
  const resource = useKeyedClientResource(key, [apiBase], load);
  return resource;
}
