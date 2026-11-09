import { shouldRetryCodexPoolAccountQuota, shouldRetryCodexPoolAccountTransient } from "../../backend/src/server/responses/core-codex-account";
import { consumeRecoveryFailure } from "../../backend/src/server/responses/recovery-failure";
import { fetchWithResetRetry, isNonReplayableResponse } from "../../backend/src/lib/upstream-retry";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { clearKeyCooldowns } from "../../backend/src/providers/key-failover";
import { handleResponses } from "../../backend/src/server/responses/core";
import type { RequestLogContext } from "../../backend/src/server/request-log";
import type { OcxConfig } from "../../backend/src/types";
import { DEVIN_API_SERVER } from "../../backend/src/adapters/devin";
import { setCachedCatalogForTests } from "../../backend/src/adapters/devin/cloud-direct/catalog";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { removeTreeWithRetry } from "../helpers/remove-tree";
import { acquireOwnedSpendHome } from "../helpers/owned-spend-home";
import { saveCredential } from "../../backend/src/oauth/store";
import { createRequestExecutionBudget } from "../../backend/src/lib/request-execution-budget";

/**
 * One logical request, one send budget -- asserted as a COUNT, because the defect in #4546 is a
 * count. Every layer that can re-send bounded itself correctly and the layers multiplied, so the
 * only assertion that catches a regression here is the exact number of times the proxy reached
 * upstream for one client turn.
 *
 * These rows use a key-auth `openai-chat` provider with `transientRetryOn5xx` because that is the
 * counted path: the generic adapter branch draws `attempts` from the request budget and reports
 * every physical send back through `onSendsConsumed`, and `noteAttemptSend` records the same send
 * on the attempt. An adapter without an opted-in transient policy keeps reset-only semantics and
 * hops on the first 5xx, so it would pin a 1 for every shape and prove nothing.
 */
const originalFetch = globalThis.fetch;

// Every dispatching row below calls the handler directly, so it takes the spend-journal writer
// lease that startServer would have taken for it. Per row rather than per file: two rows install
// their own OPENCODEX_HOME, and a lease is bound to the directory in effect when it was taken.
// The teardown drop is a backstop for a row that throws mid-assertion, because a lease left
// behind makes the NEXT row's different home read as an ownership conflict rather than as this
// row's failure.
let releaseSpendHome: (() => void) | undefined;
const takeSpendHome = (): void => { releaseSpendHome = acquireOwnedSpendHome(); };
const dropSpendHome = (): void => { releaseSpendHome?.(); releaseSpendHome = undefined; };

beforeEach(() => {
  clearKeyCooldowns();
});

afterEach(() => {
  dropSpendHome();
  globalThis.fetch = originalFetch;
  setCachedCatalogForTests(null);
  clearKeyCooldowns();
});

function transientChatProvider(name: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    adapter: "openai-chat",
    baseUrl: `https://${name}.example/v1`,
    authMode: "key",
    apiKey: `sk-${name}`,
    models: [`model-${name}`],
    transientRetryOn5xx: { enabled: true, attempts: 3 },
    ...extra,
  };
}

/** A failover combo over `count` distinct single-model providers, each on the counted path. */
function comboOverTargets(count: number): OcxConfig {
  const providers: Record<string, unknown> = {};
  const targets: Array<{ provider: string; model: string }> = [];
  for (let index = 0; index < count; index++) {
    const name = `t${index}`;
    providers[name] = transientChatProvider(name);
    targets.push({ provider: name, model: `model-${name}` });
  }
  return {
    defaultProvider: "t0",
    providers,
    combos: { fan: { strategy: "failover", targets } },
  } as unknown as OcxConfig;
}

function responsesRequest(model: string): Request {
  return new Request("http://localhost/v1/responses", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model, stream: false, input: "hello" }),
  });
}

function alwaysFailing(status: number, message: string): { authorizations: string[] } {
  const authorizations: string[] = [];
  globalThis.fetch = (async (_input: string | URL | Request, init?: RequestInit) => {
    authorizations.push(new Headers(init?.headers).get("authorization") ?? "");
    return new Response(JSON.stringify({ error: { message, type: "server_error" } }), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return { authorizations };
}

const sendCounts = (logCtx: RequestLogContext): number[] =>
  (logCtx.attempts ?? []).map(attempt => attempt.sendCount);

const totalSends = (logCtx: RequestLogContext): number =>
  sendCounts(logCtx).reduce((sum, count) => sum + count, 0);

describe("upstream sends per logical request", () => {
  test("Devin's initial inner send is recorded once, not omitted or double-counted", async () => {
    const previousHome = process.env.OPENCODEX_HOME;
    const previousJwtFlag = process.env.OPENCODEX_DEVIN_SEND_USER_JWT;
    const home = mkdtempSync(join(tmpdir(), "devin-send-count-"));
    process.env.OPENCODEX_HOME = home;
    // Taken on the home this row just installed, and dropped in its finally before that home
    // is removed: an open lease inside a directory being deleted fails the removal on Windows.
    takeSpendHome();
    delete process.env.OPENCODEX_DEVIN_SEND_USER_JWT;
    const apiKey = "devin-count-test";
    // Devin is an OAuth-kind provider: the key the adapter ends up using is injected onto the
    // row from the stored credential, so a config that only carries `apiKey` never routes. The
    // credential is what makes this the path production takes.
    await saveCredential("devin", {
      access: apiKey,
      refresh: apiKey,
      expires: Number.MAX_SAFE_INTEGER,
      source: "oauth",
      apiBaseUrl: DEVIN_API_SERVER,
    });
    setCachedCatalogForTests({
      apiKey,
      host: DEVIN_API_SERVER,
      fetchedAt: Date.now(),
      byUid: new Map([["swe-2", { modelUid: "swe-2", label: "SWE-2", disabled: false }]]),
    });
    const urls: string[] = [];
    globalThis.fetch = (async input => {
      urls.push(String(input));
      return new Response("busy", { status: 500 });
    }) as typeof fetch;
    const logCtx: RequestLogContext = { model: "", provider: "" };
    const config = {
      defaultProvider: "devin",
      providers: {
        devin: {
          adapter: "devin", baseUrl: DEVIN_API_SERVER, models: ["swe-2"],
        },
      },
    } as unknown as OcxConfig;

    try {
      const response = await handleResponses(responsesRequest("devin/swe-2"), config, logCtx);
      const body = await response.text();

      // Reported together, with the status, so a turn that never reaches the adapter says so
      // instead of presenting as an empty URL list.
      expect({
        chatCalls: urls.filter(url => url.includes("GetChatMessage")).length,
        totalSends: totalSends(logCtx),
      }, `status ${response.status}: ${body.slice(0, 200)}`).toEqual({ chatCalls: 1, totalSends: 1 });
    } finally {
      dropSpendHome();
      if (previousHome === undefined) delete process.env.OPENCODEX_HOME;
      else process.env.OPENCODEX_HOME = previousHome;
      if (previousJwtFlag === undefined) delete process.env.OPENCODEX_DEVIN_SEND_USER_JWT;
      else process.env.OPENCODEX_DEVIN_SEND_USER_JWT = previousJwtFlag;
      removeTreeWithRetry(home);
    }
  });

  test("a Devin turn the budget refuses logs no send at the request boundary", async () => {
    // The defect this pins lives in the outer runTurn path, not in the adapter: the attempt's
    // first send was logged before the adapter ran, so a request with nothing left to spend
    // recorded a send Devin never made. The direct-adapter case in tests/adapters covers the
    // executor side; only this one can see `sendCount`. The budget is handed in already spent
    // rather than arranged through combo arithmetic, which is how an earlier attempt at this
    // case ended up admitting the send it meant to refuse.
    const previousHome = process.env.OPENCODEX_HOME;
    const previousJwtFlag = process.env.OPENCODEX_DEVIN_SEND_USER_JWT;
    const home = mkdtempSync(join(tmpdir(), "devin-send-denied-"));
    process.env.OPENCODEX_HOME = home;
    takeSpendHome();
    delete process.env.OPENCODEX_DEVIN_SEND_USER_JWT;
    const apiKey = "devin-denied-test";
    await saveCredential("devin", {
      access: apiKey,
      refresh: apiKey,
      expires: Number.MAX_SAFE_INTEGER,
      source: "oauth",
      apiBaseUrl: DEVIN_API_SERVER,
    });
    setCachedCatalogForTests({
      apiKey,
      host: DEVIN_API_SERVER,
      fetchedAt: Date.now(),
      byUid: new Map([["swe-2", { modelUid: "swe-2", label: "SWE-2", disabled: false }]]),
    });
    const urls: string[] = [];
    globalThis.fetch = (async input => {
      urls.push(String(input));
      return new Response(JSON.stringify({ error: { message: "busy", type: "server_error" } }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }) as typeof fetch;
    const logCtx: RequestLogContext = { model: "", provider: "" };
    const config = {
      defaultProvider: "devin",
      providers: { devin: { adapter: "devin", baseUrl: DEVIN_API_SERVER, models: ["swe-2"] } },
    } as unknown as OcxConfig;
    // The real budget factory with nothing to give: the state an earlier combo fan-out or
    // empty-response recovery leaves behind, stated directly instead of inferred.
    const spent = createRequestExecutionBudget({
      maxTotalModelSends: 0,
      baseSendAllowance: 0,
      finalRecoveryAllowance: 0,
      maxAlternateTargetSends: 0,
      maxTargetTransitions: 0,
    }, "devin-denied-initial-send");

    try {
      const response = await handleResponses(
        responsesRequest("devin/swe-2"), config, logCtx, { sendBudget: spent },
      );
      const body = await response.text();
      const attempts = logCtx.attempts ?? [];

      // The attempt must EXIST and be empty. An absent attempt would satisfy a zero count
      // without proving the refusal was recorded against the turn that was refused.
      expect(attempts).toHaveLength(1);
      expect({
        // Pinned, not merely reported in the failure message. The refusal reaches the client as
        // an error code on a buffered FAILED response, so the outer HTTP status stays 200; a
        // reader who assumes 429 here would be describing a transport this path never uses.
        status: response.status,
        adapter: attempts[0]?.adapter,
        sendCount: attempts[0]?.sendCount,
        chatCalls: urls.filter(url => url.includes("GetChatMessage")).length,
        totalSends: totalSends(logCtx),
        refused: body.includes("request_send_budget_exhausted"),
      }, `status ${response.status}: ${body.slice(0, 240)}`).toEqual({
        status: 200,
        adapter: "devin",
        sendCount: 0,
        chatCalls: 0,
        totalSends: 0,
        refused: true,
      });
    } finally {
      dropSpendHome();
      if (previousHome === undefined) delete process.env.OPENCODEX_HOME;
      else process.env.OPENCODEX_HOME = previousHome;
      if (previousJwtFlag === undefined) delete process.env.OPENCODEX_DEVIN_SEND_USER_JWT;
      else process.env.OPENCODEX_DEVIN_SEND_USER_JWT = previousJwtFlag;
      setCachedCatalogForTests(null);
      removeTreeWithRetry(home);
    }
  });

  test("a 5xx streak on a single target spends the base allowance and stops", async () => {
    const upstream = alwaysFailing(502, "upstream busy");
    const logCtx: RequestLogContext = { model: "", provider: "" };

    takeSpendHome();
    const response = await handleResponses(
      responsesRequest("t0/model-t0"),
      { defaultProvider: "t0", providers: { t0: transientChatProvider("t0") } } as unknown as OcxConfig,
      logCtx,
    );

    expect(response.status).toBe(502);
    await response.text();
    // Three same-target sends is the guarded profile's base allowance. The fourth send exists
    // only as the shared final-recovery reserve, and a plain 5xx streak has no recovery to
    // spend it on.
    expect(upstream.authorizations).toHaveLength(3);
    expect(totalSends(logCtx)).toBe(3);
  });

  // REMOVED: "a 401 before the 5xx streak spends one of the same three sends".
  //
  // The row asserted a key rotation this harness never performs: the fixture records exactly one
  // physical send, so authorizations[1] is undefined and the logCtx total is 1. Keeping it would
  // have pinned a path the test does not reach. The property it was meant to cover -- a credential
  // hop draws on the shared remainder instead of re-arming its own allowance -- is pinned directly
  // at the budget in tests/lib/execution-budget-permits.test.ts, where the roster walk and the
  // cross-pool move are both asserted. Restoring an end-to-end row needs a harness that actually
  // rotates, which is its own change.
});

describe("ambiguous reset safety across Responses recovery", () => {
  for (const adapter of ["openai-chat", "openai-responses"]) {
    for (const combo of [false, true]) {
    }
  }

  test("an OpenCode Go destination refuses an ambiguous pre-answer reset instead of replaying", async () => {
    // The removed replaySafe exception let the first send to this destination retry a
    // dropped inference once. With it gone the destination behaves like every other:
    // reset before the answer -> refusal 429, exactly one send on the wire.
    const config = {
      defaultProvider: "go",
      providers: { go: transientChatProvider("go", { baseUrl: "https://opencode.ai/zen/go/v1" }) },
    } as unknown as OcxConfig;
    let sends = 0;
    globalThis.fetch = (async () => {
      sends += 1;
      throw Object.assign(new Error("The socket connection was closed unexpectedly."), { code: "ECONNRESET" });
    }) as typeof fetch;
    const logCtx: RequestLogContext = { model: "", provider: "" };
    takeSpendHome();
    const response = await handleResponses(responsesRequest("go/model-go"), config, logCtx);
    expect(response.status).toBe(429);
    expect((await response.json()).error.code).toBe("upstream_reset_replay_refused");
    expect(sends).toBe(1);
  });
});

describe("ambiguous reset safety after outer recovery", () => {

  // The row above arms ONE same-target attempt, so the refusal it produces arrives with the
  // arm already spent and nothing left to replay it. That is the case the guard at the top of
  // the recovery loop already covered. The defect is the arm that still has an attempt left:
  // the refusal is itself a 429, the while condition is still true, and the next attempt sends
  // the turn a third time -- the exact duplicate inference the refusal exists to prevent.
  for (const adapter of ["openai-chat", "openai-responses"]) {
    test(`${adapter}: a second same-target 429 attempt cannot replay the refusal`, async () => {
      const config = comboOverTargets(2);
      for (const provider of Object.values(config.providers)) provider.adapter = adapter;
      // Two attempts, not one: the first consumes the real rate limit, the second is the arm
      // that must NOT fire once the refetch has been refused.
      config.providers.t0!.retryOn429 = { attempts: 2 };
      const authorizations: string[] = [];
      globalThis.fetch = (async (_input: string | URL | Request, init?: RequestInit) => {
        authorizations.push(new Headers(init?.headers).get("authorization") ?? "");
        if (authorizations.length === 1) return new Response("rate limited", {
          status: 429, headers: { "retry-after": "0" },
        });
        throw Object.assign(new Error("connection reset by peer"), { code: "ECONNRESET" });
      }) as typeof fetch;
      const logCtx: RequestLogContext = { model: "", provider: "" };
      takeSpendHome();
      const response = await handleResponses(responsesRequest("t0/model-t0"), config, logCtx);

      expect(response.status).toBe(429);
      expect((await response.json()).error.code).toBe("upstream_reset_replay_refused");
      // Exactly two: the rate-limited send and the refetch that was refused. A third entry is
      // the regression, and the base allowance (3) can afford it, so this count is the proof.
      expect(authorizations).toEqual(["Bearer sk-t0", "Bearer sk-t0"]);
      expect(totalSends(logCtx)).toBe(2);
    });
  }
});
