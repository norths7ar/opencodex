import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { handleManagementAPI } from "../../src/server/management-api";
import { readConfigDiagnostics, saveConfig } from "../../src/config";
import { clearFallbackEventsForTests, guardFallbackHop } from "../../src/routing/fallback-policy";
import type { OcxConfig } from "../../src/types";
import { catalogConvergenceFactory } from "../helpers/catalog-convergence";
import { createTempHome, type TempHome } from "../helpers/temp-home";

const KEY = "key_fixture";
let home: TempHome;

function config(): OcxConfig {
  return {
    port: 10100,
    defaultProvider: "alpha",
    apiKeys: [
      { id: KEY, name: "fixture", key: "fixture-key", createdAt: new Date(0).toISOString() },
    ],
    providers: { alpha: { adapter: "openai-chat", baseUrl: "https://alpha.test/v1", models: ["m1"] } },
  } as OcxConfig;
}

async function call(c: OcxConfig, body?: unknown): Promise<Response> {
  const req = new Request("http://localhost/api/fallback", {
    method: body === undefined ? "GET" : "PUT",
    headers: { ...(body === undefined ? {} : { "content-type": "application/json" }), host: "localhost" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const response = await handleManagementAPI(req, new URL(req.url), c, {
    saveConfigPreservingClaudeCode: () => {}, createManagementConvergeCodex: catalogConvergenceFactory(),
  });
  if (!response) throw new Error("route not handled");
  return response;
}

beforeEach(() => {
  home = createTempHome("ocx-fallback-routes-");
  clearFallbackEventsForTests();
});
afterEach(() => {
  clearFallbackEventsForTests();
  home.remove();
});

describe("GET /api/fallback", () => {
  test("an unconfigured proxy reports every fallback on", async () => {
    const response = await call(config());
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({
      schema: "ocx.fallback/1",
      global: { model: true, provider: true },
      providers: {},
      routes: {},
      devices: [],
      last: null,
      recent: [],
    });
  });

});

describe("PUT /api/fallback", () => {

  test("a configured API key id is a device; an unknown device and malformed bodies are refused", async () => {
    const live = config();
    saveConfig(live);
    expect((await call(live, { scope: "device", key: KEY, setting: "model", value: false })).status).toBe(200);
    expect(live.fallback?.devices?.[KEY]).toEqual({ model: false });
    expect((await call(live, { scope: "device", key: "nobody", setting: "model", value: false })).status).toBe(404);
    // Clearing a device that is gone (a revoked link keeps its entry) is allowed and removes it.
    live.fallback = { devices: { retired_fixture: { model: false }, [KEY]: { model: false } } };
    saveConfig(live);
    expect((await call(live, { scope: "device", key: "retired_fixture", setting: "model", value: null })).status).toBe(200);
    expect(live.fallback).toEqual({ devices: { [KEY]: { model: false } } });
    expect(readConfigDiagnostics().config.fallback).toEqual({ devices: { [KEY]: { model: false } } });
    for (const body of [
      { scope: "global", setting: "localFallback", value: true },
      { scope: "provider", setting: "model", value: false },
      { scope: "route", key: "has space", setting: "model", value: false },
      { scope: "global", setting: "account", value: false },
      { scope: "global", setting: "model", value: "off" },
      { writes: [] },
      { writes: [{ scope: "global", setting: "model", value: false }, { scope: "global", setting: "bogus", value: false }] },
    ]) expect((await call(live, body)).status).toBe(400);
    // A refused batch writes nothing.
    expect(live.fallback?.model).toBeUndefined();
  });

  test("a batch applies every write in one mutation, and an unknown device refuses the whole batch", async () => {
    const live = config();
    saveConfig(live);
    expect((await call(live, { writes: [
      { scope: "device", key: KEY, setting: "model", value: false },
      { scope: "device", key: KEY, setting: "provider", value: false },
    ] })).status).toBe(200);
    expect(live.fallback).toEqual({ devices: { [KEY]: { model: false, provider: false } } });
    expect((await call(live, { writes: [
      { scope: "global", setting: "model", value: false },
      { scope: "device", key: "nobody", setting: "model", value: false },
    ] })).status).toBe(404);
    expect(readConfigDiagnostics().config.fallback).toEqual({ devices: { [KEY]: { model: false, provider: false } } });
  });
});
