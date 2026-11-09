import { describe, expect, test } from "bun:test";
import { cmdClearCooldown, cmdPause } from "../../src/cli/account-extended";
import type { AccountDeps } from "../../src/cli/account-api";

interface Captured {
  method: string;
  path: string;
  body: unknown;
}

function deps(
  respond: (captured: Captured) => { status?: number; json: unknown },
  calls: Captured[],
): AccountDeps {
  return {
    baseUrl: "http://127.0.0.1:10100",
    loadConfigImpl: () => ({ providers: { openai: { adapter: "codex" } } }) as never,
    fetchImpl: (async (url: string | URL | Request, init?: RequestInit) => {
      const captured: Captured = {
        method: init?.method ?? "GET",
        path: new URL(String(url)).pathname,
        body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      };
      calls.push(captured);
      if (captured.method === "GET" && captured.path === "/api/codex-auth/accounts") {
        // Pause and cooldown resolve their account argument against the list before writing.
        return new Response(JSON.stringify({ accounts: KNOWN_ACCOUNTS.map(id => ({ id })) }), { status: 200 });
      }
      if (captured.method === "GET" && captured.path === "/api/oauth/accounts") {
        return new Response(JSON.stringify({ accounts: [{ id: "acct_1", alias: "gem-pro" }] }), { status: 200 });
      }
      const { status = 200, json } = respond(captured);
      return new Response(JSON.stringify(json), { status });
    }) as unknown as typeof fetch,
  };
}

// Ids the mocked account list answers with; "nope" stays listed so the 404 test still reaches the server.
const KNOWN_ACCOUNTS = ["acct_1", "acct_2", "nope"];

function capture(): { lines: string[]; errors: string[]; restore: () => void } {
  const lines: string[] = [];
  const errors: string[] = [];
  const log = console.log;
  const err = console.error;
  console.log = (...a: unknown[]) => { lines.push(a.map(String).join(" ")); };
  console.error = (...a: unknown[]) => { errors.push(a.map(String).join(" ")); };
  return { lines, errors, restore: () => { console.log = log; console.error = err; } };
}

describe("ocx account pause / resume", () => {
  test("Anthropic ambiguous aliases fail without a write; exact ids still win", async () => {
    const out = capture();
    const writes: unknown[] = [];
    const d: AccountDeps = {
      baseUrl: "http://127.0.0.1:10100",
      loadConfigImpl: () => ({ providers: { anthropic: { adapter: "anthropic", authMode: "oauth" } } }) as never,
      fetchImpl: (async (_url, init) => {
        if (init?.method === "PUT") { writes.push(JSON.parse(String(init.body))); return Response.json({ ok: true }); }
        return Response.json({ accounts: [{ id: "a", alias: "reserve" }, { id: "b", alias: "reserve" }] });
      }) as typeof fetch,
    };
    try {
      expect(await cmdPause(["anthropic", "reserve"], d, true)).toBe(1);
      expect(writes).toEqual([]);
      expect(await cmdPause(["anthropic", "a"], d, true)).toBe(0);
      expect(writes).toEqual([{ provider: "anthropic", accountId: "a", paused: true }]);
    } finally { out.restore(); }
  });

  test.each([true, false])("Anthropic pause=%s resolves a unique alias and preserves the JSON contract", async paused => {
    const calls: Captured[] = [];
    const out = capture();
    const d = deps(() => ({ json: { ok: true, activeAccountId: "acct_2" } }), calls);
    d.loadConfigImpl = () => ({ providers: { anthropic: { adapter: "anthropic", authMode: "oauth" } } }) as never;
    try {
      expect(await cmdPause(["anthropic", "GEM-PRO", "--json"], d, paused)).toBe(0);
      expect(calls.find(call => call.method === "PUT")?.body).toEqual({ provider: "anthropic", accountId: "acct_1", paused });
      expect(JSON.parse(out.lines.join("\n"))).toEqual({ ok: true, provider: "anthropic", id: "acct_1", paused, activeAccountId: "acct_2" });
    } finally { out.restore(); }
  });

  test("generic OAuth pause resolves aliases and uses the OAuth account route", async () => {
    const calls: Captured[] = [];
    const out = capture();
    const base = deps(() => ({ json: { ok: true } }), calls);
    const genericDeps: AccountDeps = {
      ...base,
      loadConfigImpl: () => ({ providers: { "google-antigravity": { adapter: "google", baseUrl: "https://cloudcode-pa.googleapis.com", authMode: "oauth" } } }) as never,
    };
    let code: number;
    try {
      code = await cmdPause(["google-antigravity", "gem-pro"], genericDeps, true);
    } finally { out.restore(); }
    expect(code).toBe(0);
    const write = calls.find(call => call.path === "/api/oauth/accounts/pause");
    expect(write?.method).toBe("PUT");
    expect(write?.body).toEqual({ provider: "google-antigravity", accountId: "acct_1", paused: true });
  });

  test("generic OAuth resume reports when it changes the active account", async () => {
    const calls: Captured[] = [];
    const out = capture();
    const base = deps(() => ({ json: { ok: true, activeAccountChanged: true, activeAccountId: "acct_2" } }), calls);
    const genericDeps: AccountDeps = {
      ...base,
      loadConfigImpl: () => ({ providers: { "google-antigravity": { adapter: "google", baseUrl: "https://cloudcode-pa.googleapis.com", authMode: "oauth" } } }) as never,
    };
    try {
      await cmdPause(["google-antigravity", "acct_1"], genericDeps, false);
    } finally { out.restore(); }
    expect(out.errors.join("\n")).toContain("Active account changed to acct_2.");
  });

  test("pause PUTs the shared route with paused true", async () => {
    const calls: Captured[] = [];
    const out = capture();
    let code: number;
    try {
      code = await cmdPause(["openai", "acct_1"], deps(() => ({ json: { ok: true } }), calls), true);
    } finally { out.restore(); }
    expect(code).toBe(0);
    const write = calls.find(call => call.path === "/api/codex-auth/accounts/pause");
    // PUT, not POST: the issue text says POST and the server implements PUT.
    expect(write?.method).toBe("PUT");
    expect(write?.body).toEqual({ id: "acct_1", paused: true });
    expect(out.lines.join("\n")).toContain("paused");
  });

  test("resume uses the SAME route with paused false", async () => {
    const calls: Captured[] = [];
    const out = capture();
    try {
      await cmdPause(["openai", "acct_1"], deps(() => ({ json: { ok: true } }), calls), false);
    } finally { out.restore(); }
    const write = calls.find(call => call.path === "/api/codex-auth/accounts/pause");
    expect(write?.body).toEqual({ id: "acct_1", paused: false });
    expect(out.lines.join("\n")).toContain("resumed");
  });

  test("main resolves to the main account id", async () => {
    const calls: Captured[] = [];
    const out = capture();
    try {
      await cmdPause(["openai", "main"], deps(() => ({ json: { ok: true } }), calls), true);
    } finally { out.restore(); }
    expect((calls[0]?.body as { id: string }).id).not.toBe("main");
  });

  test("pausing warns about unbinding and fallback selection", async () => {
    // Both are server-side effects of this route, not consequences an operator would infer
    // from the word "pause".
    const out = capture();
    try {
      await cmdPause(["openai", "acct_1"], deps(() => ({ json: { ok: true } }), []), true);
    } finally { out.restore(); }
    expect(out.errors.join("\n")).toContain("unbound");
  });

  test("resuming does not print the pause side-effect warning", async () => {
    const out = capture();
    try {
      await cmdPause(["openai", "acct_1"], deps(() => ({ json: { ok: true } }), []), false);
    } finally { out.restore(); }
    expect(out.errors.join("\n")).not.toContain("unbound");
  });

  test("a 404 surfaces the server's reason and a non-zero code", async () => {
    const out = capture();
    let code: number;
    try {
      code = await cmdPause(["openai", "nope"], deps(() => ({ status: 404, json: { error: "Account not found" } }), []), true);
    } finally { out.restore(); }
    expect(code).not.toBe(0);
    expect(out.errors.join("\n")).toContain("Account not found");
  });
});

describe("ocx account clear-cooldown", () => {
  test("Codex keeps its dedicated route and body", async () => {
    const calls: Captured[] = [];
    const out = capture();
    try {
      expect(await cmdClearCooldown(["openai", "acct_1"], deps(() => ({ json: { ok: true, cleared: true } }), calls))).toBe(0);
    } finally { out.restore(); }
    expect(calls.at(-1)).toEqual({
      method: "POST",
      path: "/api/codex-auth/accounts/clear-cooldown",
      body: { id: "acct_1" },
    });
  });

  test("Anthropic resolves an alias and posts to the OAuth cooldown owner", async () => {
    const calls: Captured[] = [];
    const out = capture();
    const anthropicDeps: AccountDeps = {
      baseUrl: "http://127.0.0.1:10100",
      loadConfigImpl: () => ({ providers: { anthropic: {} } }) as never,
      fetchImpl: (async (url: string | URL | Request, init?: RequestInit) => {
        const parsed = new URL(String(url));
        const call: Captured = {
          method: init?.method ?? "GET",
          path: parsed.pathname + parsed.search,
          body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
        };
        calls.push(call);
        const json = call.method === "GET"
          ? { accounts: [{ id: "anthropic_1", alias: "work" }] }
          : { ok: true, cleared: true };
        return Response.json(json);
      }) as typeof fetch,
    };
    try {
      expect(await cmdClearCooldown(["anthropic", "work"], anthropicDeps)).toBe(0);
    } finally { out.restore(); }
    expect(calls).toEqual([
      { method: "GET", path: "/api/oauth/accounts?provider=anthropic", body: undefined },
      {
        method: "POST",
        path: "/api/oauth/accounts/clear-cooldown",
        body: { provider: "anthropic", accountId: "anthropic_1" },
      },
    ]);
    expect(out.lines.join("\n")).toContain("anthropic: cooldown lifted for work");
  });

  test("unrelated OAuth providers are rejected before any management request", async () => {
    const calls: Captured[] = [];
    const out = capture();
    let code: number;
    try {
      code = await cmdClearCooldown(["google-antigravity", "acct_1"], {
        baseUrl: "http://127.0.0.1:10100",
        loadConfigImpl: () => ({ providers: { "google-antigravity": { authMode: "oauth" } } }) as never,
        fetchImpl: (async () => { calls.push({ method: "GET", path: "unexpected", body: undefined }); return Response.json({}); }) as typeof fetch,
      });
    } finally { out.restore(); }
    expect(code).toBe(1);
    expect(calls).toHaveLength(0);
    expect(out.errors.join("\n")).toContain("no operator-clearable account cooldown");
  });
});
