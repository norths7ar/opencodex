import { describe, expect, test } from "vitest";
import { requestCodexRestart } from "../src/codex-restart";
import type { CodexRestartResponse } from "../src/codex-restart";

const STOPPED: CodexRestartResponse = {
  success: true,
  stateBefore: "stale",
  synced: true,
  requested: [4242],
  stopped: [4242],
  surviving: [],
  failed: [],
  code: "stopped",
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const formatters = {
  formatFailure: (status: number) => `http ${status}`,
  formatUnreachable: () => "unreachable",
  formatMalformed: () => "malformed",
  formatTimeout: () => "timeout",
};

describe("requestCodexRestart", () => {
  test("returns the parsed contract body on success", async () => {
    const outcome = await requestCodexRestart("", {
      fetchFn: (async () => response(STOPPED)) as typeof fetch,
      ...formatters,
    });

    expect(outcome.ok).toBe(true);
    expect(outcome.result).toEqual(STOPPED);
  });

  test("a failed restart reports the error and an explicit retry can succeed", async () => {
    let available = false;
    const options = {
      fetchFn: (async () =>
        available ? response(STOPPED) : response({ error: "nope" }, 500)) as typeof fetch,
      ...formatters,
    };
    const outcome = await requestCodexRestart("", options);

    expect(outcome).toEqual({ ok: false, message: "http 500" });
    available = true;
    expect((await requestCodexRestart("", options)).result).toEqual(STOPPED);
  });

  test("a network failure is reported as unreachable", async () => {
    // requestProxyStop resolves a dropped socket by re-reading the instance, because the
    // process it talks to is the one going away. This route does not kill the process
    // serving it, so there is nothing to re-read and silence means something broke.
    const outcome = await requestCodexRestart("", {
      fetchFn: (async () => {
        throw new TypeError("Failed to fetch");
      }) as typeof fetch,
      ...formatters,
    });

    expect(outcome).toEqual({ ok: false, message: "unreachable" });
  });

  test("a timeout is reported separately, because the work may still be running", async () => {
    // The proxy never sees the client abort: a long catalog sync can still stop
    // app-servers afterwards. Saying "could not reach the proxy" would be a lie
    // the user acts on.
    const outcome = await requestCodexRestart("", {
      fetchFn: (async () => {
        throw new DOMException("The operation timed out.", "TimeoutError");
      }) as typeof fetch,
      ...formatters,
    });

    expect(outcome).toEqual({ ok: false, message: "timeout" });
  });

  test("rejects a parseable 2xx body of the wrong shape", async () => {
    // The handler reads .stopped.length; a bare { success: true } would throw
    // inside an event handler, where it surfaces as a dead button.
    const outcome = await requestCodexRestart("", {
      fetchFn: (async () => response({ success: true })) as typeof fetch,
      ...formatters,
    });

    expect(outcome).toEqual({ ok: false, message: "malformed" });
  });
});
