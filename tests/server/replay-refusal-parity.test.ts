import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { saveConfig } from "../../src/config";
import { startServer } from "../../src/server";
import {
  REPLAY_REFUSAL_NO_RETRY_HEADER,
  REPLAY_REFUSAL_NO_RETRY_VALUE,
  REPLAY_REFUSED_STATUS,
  UPSTREAM_RESET_REPLAY_REFUSED_CODE,
} from "../../src/lib/upstream-retry";
import { DEFAULT_RETRYABLE_429_RETRY_AFTER_SEC } from "../../src/lib/retry-after";
import { installIsolatedCodexHome, type IsolatedCodexHome } from "../helpers/isolated-codex-home";
import { removeTreeWithRetry } from "../helpers/remove-tree";
import type { OcxConfig } from "../../src/types";

/**
 * The acceptance unit for the ambiguous-resend refusal is not the shape of one response: it is
 * how many times the turn physically reaches upstream when a real client is allowed to retry.
 * A single `fetch` cannot see that, because a client with retries enabled is the thing that
 * resends -- the proxy answered correctly and the duplicate inference happened anyway.
 *
 * So these cases run the proxy over a real socket, count the sends at the upstream boundary,
 * and drive it with a client that retries the way the published SDKs do. The four surfaces
 * are asserted against one expectation because a client cannot tell them apart: it sent one
 * turn and the turn may already have executed, whichever endpoint carried it.
 */
const UPSTREAM_HOST = "replay-refusal-parity.example.test";
const originalFetch = globalThis.fetch;
let testDir = "";
let previousHome: string | undefined;
let isolatedCodexHome: IsolatedCodexHome | null = null;

beforeEach(() => {
  previousHome = process.env.OPENCODEX_HOME;
  isolatedCodexHome = installIsolatedCodexHome("ocx-replay-refusal-");
  testDir = mkdtempSync(join(tmpdir(), "ocx-replay-refusal-"));
  process.env.OPENCODEX_HOME = testDir;
  globalThis.fetch = originalFetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (previousHome === undefined) delete process.env.OPENCODEX_HOME;
  else process.env.OPENCODEX_HOME = previousHome;
  isolatedCodexHome?.restore();
  isolatedCodexHome = null;
  if (testDir) removeTreeWithRetry(testDir);
});

/**
 * The retry rule of the official clients, written as they write it.
 *
 * The header name and its two accepted values are deliberately literals here rather than the
 * constants this repository exports. This function stands in for the third party: it has to
 * keep believing what `openai` and `anthropic` believe -- an explicit verdict first, then the
 * 408/409/429/5xx table -- even if our own constant were changed to something no client reads.
 */
function sdkWouldRetry(response: Response): boolean {
  const verdict = response.headers.get("x-should-retry");
  if (verdict === "true") return true;
  if (verdict === "false") return false;
  return response.status === 408 || response.status === 409
    || response.status === 429 || response.status >= 500;
}

/** One logical request through a client whose retries are enabled. */
async function sendWithClientRetries(
  url: URL,
  body: Record<string, unknown>,
  maxRetries = 2,
): Promise<{ response: Response; attempts: number; json: { error?: { code?: string } } }> {
  let attempts = 0;
  for (;;) {
    attempts += 1;
    const response = await originalFetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (attempts > maxRetries || !sdkWouldRetry(response)) {
      return { response, attempts, json: await response.json() as { error?: { code?: string } } };
    }
    // Release the body before the next attempt, as the SDKs do.
    await response.text();
  }
}

/**
 * Count physical upstream sends and answer each one as the fixture dictates. Everything not
 * addressed to the fixture host -- the client's own calls included -- keeps the real fetch.
 */
function countingUpstream(answer: () => Response): () => number {
  let sends = 0;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input
      : input instanceof URL ? input.href
        : input.url;
    if (!url.includes(UPSTREAM_HOST)) return originalFetch(input as RequestInfo, init);
    sends += 1;
    return answer();
  }) as typeof fetch;
  return () => sends;
}

/** A half-closed pooled socket: the request has left, and nothing comes back. */
function preHeaderReset(): never {
  throw Object.assign(
    new Error("The socket connection was closed unexpectedly."),
    { code: "ECONNRESET" },
  );
}

function parityConfig(): OcxConfig {
  const provider = (apiKey: string, adapter: string) => ({
    adapter,
    baseUrl: `https://${UPSTREAM_HOST}/v1`,
    authMode: "key",
    apiKey,
    models: ["model"],
  });
  return {
    port: 0,
    defaultProvider: "native",
    providers: {
      // Native Chat keeps the caller on the Chat wire; the bridged row translates through
      // Responses and back, which is the surface that used to lose the refusal.
      native: provider("sk-native", "openai-chat"),
      bridged: provider("sk-bridged", "openai-responses"),
    },
  } as unknown as OcxConfig;
}

const CHAT_TURN = { messages: [{ role: "user", content: "ping" }] };
const CLAUDE_TURN = { max_tokens: 64, messages: [{ role: "user", content: "ping" }] };
const RESPONSES_TURN = { input: [{ role: "user", content: [{ type: "input_text", text: "ping" }] }] };

test("every HTTP surface answers an ambiguous reset with one send and no client resend", async () => {
  saveConfig(parityConfig());
  const sends = countingUpstream(preHeaderReset);
  const server = startServer(0);
  const surfaces = [
    { name: "native Chat", path: "/v1/chat/completions", body: { model: "native/model", ...CHAT_TURN } },
    { name: "translated Chat", path: "/v1/chat/completions", body: { model: "bridged/model", ...CHAT_TURN } },
    { name: "Responses", path: "/v1/responses", body: { model: "bridged/model", ...RESPONSES_TURN } },
    { name: "routed Claude Messages", path: "/v1/messages", body: { model: "bridged/model", ...CLAUDE_TURN } },
  ];
  try {
    for (const surface of surfaces) {
      const before = sends();
      const { response, attempts, json } = await sendWithClientRetries(
        new URL(surface.path, server.url),
        surface.body,
      );
      // The number this refusal exists to hold at one, per logical request.
      expect({ surface: surface.name, sends: sends() - before, attempts })
        .toEqual({ surface: surface.name, sends: 1, attempts: 1 });
      expect({ surface: surface.name, status: response.status, code: json.error?.code }).toEqual({
        surface: surface.name,
        status: REPLAY_REFUSED_STATUS,
        code: UPSTREAM_RESET_REPLAY_REFUSED_CODE,
      });
      // No wait to honour, and no automatic resend of a turn that may already have run.
      expect(response.headers.get("Retry-After")).toBeNull();
      expect(response.headers.get(REPLAY_REFUSAL_NO_RETRY_HEADER)).toBe(REPLAY_REFUSAL_NO_RETRY_VALUE);
    }
  } finally {
    await server.stop(true);
  }
});

/**
 * The control that keeps the assertion above honest. A client double that never resends would
 * pin "one send" for any answer at all, so the same client has to be shown resending a real
 * rate limit -- the answer a refusal was indistinguishable from on the translated surfaces.
 */
test("the same client still resends an ordinary upstream rate limit", async () => {
  saveConfig(parityConfig());
  const sends = countingUpstream(() => new Response(
    JSON.stringify({ error: { message: "Too many requests", type: "rate_limit_error" } }),
    { status: 429, headers: { "content-type": "application/json" } },
  ));
  const server = startServer(0);
  try {
    const { response, attempts } = await sendWithClientRetries(
      new URL("/v1/messages", server.url),
      { model: "bridged/model", ...CLAUDE_TURN },
    );
    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe(String(DEFAULT_RETRYABLE_429_RETRY_AFTER_SEC));
    expect(response.headers.get(REPLAY_REFUSAL_NO_RETRY_HEADER)).toBeNull();
    expect(attempts).toBe(3);
    expect(sends()).toBeGreaterThan(1);
  } finally {
    await server.stop(true);
  }
});

/**
 * The same refusal has to hold inside a combo. The answer to a spent replacement can keep its real
 * status (a 400 naming a context overflow) with only an in-memory marker, and the combo rebuilds a
 * failed attempt as a new response. If that dropped the marker, the combo would read the overflow
 * as target-local and send the same turn to its next target, although the first send may already
 * have run it.
 */
const COMBO_FIRST_HOST = "replay-combo-first.example.test";
const COMBO_SECOND_HOST = "replay-combo-second.example.test";

function comboReplayConfig(): OcxConfig {
  const provider = (host: string, apiKey: string, extra: Record<string, unknown> = {}) => ({
    adapter: "openai-responses",
    baseUrl: `https://${host}/v1`,
    authMode: "key",
    apiKey,
    models: ["model"],
    ...extra,
  });
  return {
    port: 0,
    defaultProvider: "first",
    providers: {
      // The first target opts in to one ambiguous-reset replacement; the second never should be sent.
      first: provider(COMBO_FIRST_HOST, "sk-combo-first", { retryOnReset: {} }),
      second: provider(COMBO_SECOND_HOST, "sk-combo-second"),
    },
    combos: { pair: { strategy: "failover", targets: [
      { provider: "first", model: "model" },
      { provider: "second", model: "model" },
    ] } },
  } as unknown as OcxConfig;
}

test("the direct path refuses replay after a spent replacement's decrypt failure", async () => {
  const config = parityConfig();
  config.providers.bridged.retryOnReset = {};
  saveConfig(config);
  let upstreamSends = 0;
  const sends = countingUpstream(() => {
    upstreamSends += 1;
    if (upstreamSends === 1) preHeaderReset();
    const failure = { type: "response.failed", response: {
      id: "resp_decrypt_failed", status: "failed",
      error: { type: "server_error", code: "upstream_server_error",
        message: "Encrypted function output content could not be decrypted or decoded." },
    } };
    return new Response(`event: response.failed\ndata: ${JSON.stringify(failure)}\n\ndata: [DONE]\n\n`, {
      headers: { "content-type": "text/event-stream" },
    });
  });
  // Canonical key-independent Fernet structure, as in the opaque-blob recovery fixtures.
  const encryptedContent = `${Buffer.concat([
    Buffer.from([0x80]), Buffer.alloc(8), Buffer.alloc(16), Buffer.alloc(16), Buffer.alloc(32),
  ]).toString("base64url")}==`;
  const server = startServer(0);
  try {
    const response = await originalFetch(new URL("/v1/responses", server.url), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: "bridged/model", store: false, stream: true,
        input: [
          { type: "function_call", call_id: "call-encrypted-output", name: "browser_capture", arguments: "{}" },
          { type: "function_call_output", call_id: "call-encrypted-output", output: [
            { type: "encrypted_content", encrypted_content: encryptedContent },
            { type: "input_text", text: "visible tool output" },
            { type: "input_image", image_url: "data:image/png;base64,AAAA", detail: "high" },
          ] },
          { role: "user", content: [{ type: "input_text", text: "continue" }] },
        ],
      }),
    });
    expect(sends()).toBe(2);
    expect(response.status).toBe(REPLAY_REFUSED_STATUS);
    expect((await response.json()).error.code).toBe(UPSTREAM_RESET_REPLAY_REFUSED_CODE);
    expect(response.headers.get(REPLAY_REFUSAL_NO_RETRY_HEADER)).toBe(REPLAY_REFUSAL_NO_RETRY_VALUE);
  } finally {
    await server.stop(true);
  }
});
