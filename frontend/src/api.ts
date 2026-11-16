import { discardResponseBody, withFetchDeadline } from "./bounded-fetch";

export const SESSION_CHANGED_EVENT = "opencodex:session-changed";
export const SESSION_UNAVAILABLE_EVENT = "opencodex:session-unavailable";
export const USAGE_SESSION_CHANGED = "opencodex:usage-session-changed";
type UsageSessionSnapshot = { generation: number; blocked: boolean };
let usageSession: UsageSessionSnapshot = { generation: 0, blocked: false };
export function usageSessionSnapshot(): UsageSessionSnapshot {
  return usageSession;
}
function changeUsageSession(blocked: boolean): void {
  usageSession = { generation: usageSession.generation + 1, blocked };
  window.dispatchEvent(new Event(USAGE_SESSION_CHANGED));
}
export function denyUsageSession(generation: number): void {
  if (usageSession.generation === generation && !usageSession.blocked) changeUsageSession(true);
}

/** A successful management mutation may change the source-facet inventory. */
export const USAGE_SOURCE_INVENTORY_CHANGED = "opencodex:usage-source-inventory-changed";

function sourceInventoryMutation(path: string | null, method: string): boolean {
  if (!path) return false;
  if (path === "/api/keys") return ["POST", "PATCH", "DELETE"].includes(method);
  if (path === "/api/keys/rotate") return method === "POST" || method === "DELETE";
  if (path === "/api/keys/rotate/commit") return method === "POST";
  return false;
}

export function sourceInventoryEventMatches(event: Event, apiBase: string): boolean {
  const detail = (event as CustomEvent<{ apiBase?: string }>).detail;
  try {
    return (
      typeof detail?.apiBase === "string" &&
      new URL(detail.apiBase || "/", window.location.href).href.replace(/\/+$/, "") ===
        new URL(apiBase || "/", window.location.href).href.replace(/\/+$/, "")
    );
  } catch {
    return false;
  }
}

interface Session {
  token: string;
  csrfToken: string;
  browserOrigin: string;
  serverOrigin: string;
}
let session: Session | null = null;
let revision = 0;
let base = "";
let originalFetch: typeof fetch;
let installed = false;
let recovery: Promise<boolean> | null = null;

export function hasApiSession(): boolean {
  return session !== null;
}

/** Each caller can leave a shared bootstrap without cancelling the other callers. */
function cancellable<T>(operation: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return operation;
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    operation.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

export function bootstrapApiSession(signal?: AbortSignal): Promise<boolean> {
  if (!recovery) {
    const expected = revision;
    recovery = withFetchDeadline(8000, undefined, async (deadline) => {
      const response = await originalFetch(`${base}/opencodex-session`, {
        signal: deadline,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      });
      if (!response.ok) throw new Error("Local session bootstrap failed");
      const data: unknown = await response.json();
      const value = data as Partial<Session> | null;
      if (
        !value ||
        typeof value.token !== "string" ||
        !value.token.startsWith("ocx_session_") ||
        typeof value.csrfToken !== "string" ||
        !value.csrfToken ||
        value.browserOrigin !== window.location.origin ||
        value.serverOrigin !== window.location.origin
      ) {
        throw new Error("Invalid local session response");
      }
      if (revision !== expected) return hasApiSession();
      session = value as Session;
      revision++;
      changeUsageSession(false);
      window.dispatchEvent(new Event(SESSION_CHANGED_EVENT));
      return true;
    })
      .catch(() => {
        if (revision === expected) {
          session = null;
          denyUsageSession(usageSession.generation);
          window.dispatchEvent(new Event(SESSION_UNAVAILABLE_EVENT));
        }
        return false;
      })
      .finally(() => {
        recovery = null;
      });
  }
  return cancellable(recovery, signal);
}

/** Install once for this local console; never attach credentials to forwarding or external URLs. */
export function installApiAuthFetch(apiBase = ""): void {
  if (installed) return;
  const target = new URL(apiBase || "/", window.location.href);
  if (
    target.origin !== window.location.origin ||
    target.pathname !== "/" ||
    target.search ||
    target.hash
  ) {
    throw new Error("The console API must use its own origin");
  }
  base = apiBase.replace(/\/+$/, "");
  originalFetch = window.fetch.bind(window);
  installed = true;
  window.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) {
      return originalFetch(input, init);
    }
    const request = new Request(input instanceof Request ? input : url, init);
    const signal = request.signal;
    signal.throwIfAborted();
    if (!session && !(await bootstrapApiSession(signal)))
      throw new Error("Local console session unavailable");
    const send = () => {
      signal.throwIfAborted();
      const headers = new Headers(request.headers);
      headers.set("X-OpenCodex-API-Key", session!.token);
      if (!["GET", "HEAD"].includes(request.method))
        headers.set("X-OpenCodex-CSRF-Token", session!.csrfToken);
      return originalFetch(
        new Request(request.clone(), { headers, credentials: "same-origin", redirect: "error" }),
      );
    };
    const expected = revision;
    let response = await send();
    if (response.status === 401) {
      signal.throwIfAborted();
      discardResponseBody(response);
      if (revision === expected) {
        session = null;
        // Start shared recovery before invalidation aborts Usage subscribers.
        const recovering = bootstrapApiSession();
        denyUsageSession(usageSession.generation);
        if (!(await cancellable(recovering, signal)))
          throw new Error("Local console session unavailable");
      } else if (!session && !(await bootstrapApiSession(signal))) {
        throw new Error("Local console session unavailable");
      }
      const retriedRevision = revision;
      response = await send();
      if (response.status === 401 && revision === retriedRevision) {
        session = null;
        revision++;
        denyUsageSession(usageSession.generation);
        window.dispatchEvent(new Event(SESSION_UNAVAILABLE_EVENT));
      }
    }
    if (response.ok && sourceInventoryMutation(url.pathname, request.method)) {
      window.dispatchEvent(
        new CustomEvent(USAGE_SOURCE_INVENTORY_CHANGED, { detail: { apiBase: base } }),
      );
    }
    return response;
  };
}
