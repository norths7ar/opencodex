export type ApiPlane = "machine" | "shared";

/** The backend declares whether this browser must supply an admin token. */
export function adminTokenPromptAllowed(): boolean {
  return (
    typeof document !== "undefined" &&
    document
      .querySelector('meta[name="opencodex-management-auth-required"]')
      ?.getAttribute("content")
      ?.trim() === "1"
  );
}

export interface ApiTarget {
  id: ApiPlane;
  baseUrl: string;
  serverOrigin: string;
  bootstrapPath: string;
}

export interface ApiTargets {
  machine: ApiTarget;
  shared: ApiTarget;
}

/** Both local control surfaces use the same gateway and session bootstrap endpoint. */
export function standaloneApiTargets(initialBase: string): ApiTargets {
  const baseUrl = initialBase.replace(/\/+$/, "");
  const serverOrigin = new URL(initialBase || "/", window.location.href).origin;
  const target = (id: ApiPlane): ApiTarget => ({
    id,
    baseUrl,
    serverOrigin,
    bootstrapPath: `${baseUrl}/opencodex-session`,
  });
  return { machine: target("machine"), shared: target("shared") };
}

export function apiBaseForPlane(plane: ApiPlane, targets: ApiTargets): string {
  return targets[plane].baseUrl;
}
