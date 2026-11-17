import { expect, test, vi } from "vitest";
import { deferred } from "./dom";

function session(token: string) {
  return Response.json({
    token,
    csrfToken: `csrf-${token}`,
    browserOrigin: location.origin,
    serverOrigin: location.origin,
  });
}

test("restart expires the session; simultaneous callers share renewal and one cancellation does not cancel the other", async () => {
  vi.resetModules();
  const renewal = deferred<Response>();
  let bootstraps = 0;
  let restarted = false;
  const writes: Request[] = [];
  const upstream = vi.fn(async (input: RequestInfo | URL) => {
    const request =
      input instanceof Request ? input : new Request(new URL(String(input), location.href));
    if (new URL(request.url).pathname === "/opencodex-session") {
      bootstraps++;
      return bootstraps === 1 ? session("ocx_session_old") : renewal.promise;
    }
    writes.push(request);
    return restarted && request.headers.get("x-opencodex-api-key") === "ocx_session_old"
      ? new Response(null, { status: 401 })
      : Response.json({ saved: true });
  });
  vi.stubGlobal("fetch", upstream);
  const { installApiAuthFetch } = await import("../src/api");
  installApiAuthFetch();
  expect((await window.fetch("/api/config")).status).toBe(200);
  restarted = true;
  const abort = new AbortController();
  const cancelled = window
    .fetch("/api/one", { signal: abort.signal })
    .catch((error: unknown) => error);
  const saved = window.fetch("/api/two", { method: "PUT", body: JSON.stringify({ value: 2 }) });
  await vi.waitFor(() => expect(bootstraps).toBe(2));
  abort.abort();
  expect(await cancelled).toBeInstanceOf(DOMException);
  renewal.resolve(session("ocx_session_new"));
  expect((await saved).status).toBe(200);
  const retry = writes.at(-1)!;
  expect(retry.url.endsWith("/api/two")).toBe(true);
  expect(retry.headers.get("x-opencodex-csrf-token")).toBe("csrf-ocx_session_new");
  expect(await retry.json()).toEqual({ value: 2 });
  expect(writes.filter((request) => request.url.endsWith("/api/one"))).toHaveLength(1);
  expect(bootstraps).toBe(2);
});

test("failed bootstrap reports unavailable, later retry recovers, and external requests never receive session credentials", async () => {
  vi.resetModules();
  let online = false;
  const outgoing: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const request =
        input instanceof Request ? input : new Request(new URL(String(input), location.href));
      if (request.url.endsWith("/opencodex-session"))
        return online ? session("ocx_session_ready") : new Response(null, { status: 503 });
      outgoing.push(request);
      return Response.json({ ready: true });
    }),
  );
  const { installApiAuthFetch, hasApiSession } = await import("../src/api");
  installApiAuthFetch();
  await expect(window.fetch("/api/config")).rejects.toThrow("session unavailable");
  expect(hasApiSession()).toBe(false);
  expect(outgoing).toHaveLength(0);
  online = true;
  expect((await window.fetch("/api/config")).status).toBe(200);
  await window.fetch("https://external.invalid/read");
  expect(outgoing.at(-1)!.headers.has("x-opencodex-api-key")).toBe(false);
  expect(outgoing.at(-1)!.headers.has("x-opencodex-csrf-token")).toBe(false);
});
