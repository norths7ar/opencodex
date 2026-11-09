import { describe, expect, test } from "bun:test";
import { parseRequest } from "../../backend/src/responses/parser";
import { routeModel } from "../../backend/src/router";
import { applyFinalRouteRequestNormalization } from "../../backend/src/server/responses/core-normalize";
import type { OcxConfig } from "../../backend/src/types";

describe("reasoning effort preserves visible thinking when summary is omitted", () => {
  test.each(["unknown", "off", ""])("invalid effort %j does not enable visibility", effort => {
    const parsed = parseRequest({ model: "test", input: [], reasoning: { effort } });
    expect(parsed.options.reasoning).toBeUndefined();
    expect(parsed.options.hideThinkingSummary).toBe(true);
  });
  test.each([{}, [], 1, null].map(effort => ({ effort })))("non-string effort %j is rejected by the wire schema", ({ effort }) => {
    expect(() => parseRequest({ model: "test", input: [], reasoning: { effort } })).toThrow("responses parse error");
  });
  test.each(["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])("validated effort %s enables raw visibility", effort => {
    const parsed = parseRequest({ model: "test", input: [], reasoning: { effort } });
    expect(parsed.options.reasoning).toBe(effort === "ultra" ? "max" : effort);
    expect(parsed.options.hideThinkingSummary).toBeUndefined();
  });
  test.each([undefined, "none", "off", "invalid"])("explicit auto is independent of effort %s", effort => {
    expect(parseRequest({ model: "test", input: [], reasoning: { effort, summary: "auto" } })
      .options.hideThinkingSummary).toBeUndefined();
  });
  test("reasoning with active effort does not default to hideThinkingSummary", () => {
    const parsed = parseRequest({
      model: "test-model",
      reasoning: { effort: "high" },
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hi" }] }],
    });
    expect(parsed.options.reasoning).toBe("high");
    expect(parsed.options.hideThinkingSummary).toBeUndefined();
  });

  test("explicit summary of none still hides thinking summary", () => {
    const parsed = parseRequest({
      model: "test-model",
      reasoning: { effort: "high", summary: "none" },
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hi" }] }],
    });
    expect(parsed.options.reasoning).toBe("high");
    expect(parsed.options.hideThinkingSummary).toBe(true);
  });

  test("omitted reasoning and omitted effort still default to hideThinkingSummary", () => {
    const parsed = parseRequest({
      model: "test-model",
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hi" }] }],
    });
    expect(parsed.options.hideThinkingSummary).toBe(true);
  });

  test("reasoning effort of none defaults to hideThinkingSummary", () => {
    const parsed = parseRequest({
      model: "test-model",
      reasoning: { effort: "none" },
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "hi" }] }],
    });
    expect(parsed.options.hideThinkingSummary).toBe(true);
  });

  test.each(["openai-chat", "kiro"] as const)("%s final route honors caller reasoning visibility", async adapter => {
    const config: OcxConfig = {
      port: 0,
      defaultProvider: "summary-route",
      providers: { "summary-route": { adapter, baseUrl: "https://example.test/v1" } },
    };
    for (const [reasoning, hidden] of [
      [{ effort: "high" }, false],
      [{ effort: "high", summary: "none" }, true],
      [undefined, true],
    ] as const) {
      const parsed = parseRequest({ model: "summary-route/test-model", input: [], reasoning });
      const route = routeModel(config, parsed.modelId);
      expect(route.provider.showThinkingSummary).toBeUndefined();
      await applyFinalRouteRequestNormalization({
        parsed,
        route,
        config,
        req: new Request("http://localhost/v1/responses"),
        logCtx: { model: parsed.modelId, provider: route.providerName },
        inboundWire: "responses",
      });
      expect(parsed.options.hideThinkingSummary).toBe(hidden);
    }
  });

  test("final route removes a rejected thinking toggle from native passthrough", async () => {
    const config: OcxConfig = {
      port: 0,
      defaultProvider: "native",
      providers: { native: {
        adapter: "openai-responses", baseUrl: "https://example.test/v1", reasoningWireFormat: "gateway-object",
        models: ["test"],
        modelCapabilities: { test: { supportsReasoning: false, supportsThinkingToggle: false, supportsReasoningEffort: false } },
      } },
    };
    const parsed = parseRequest({ model: "native/test", input: [], reasoning: { enabled: true, effort: "high", summary: "auto" } });
    const route = routeModel(config, parsed.modelId);
    await applyFinalRouteRequestNormalization({
      parsed, route, config, req: new Request("http://localhost/v1/responses"),
      logCtx: { model: parsed.modelId, provider: route.providerName }, inboundWire: "responses",
    });
    expect(parsed.options.thinkingEnabled).toBeUndefined();
    expect((parsed._rawBody as { reasoning?: unknown }).reasoning).toEqual({ summary: "auto" });
  });

  test("Chat final-route normalization strips both shapes without losing summary or later-route effort", async () => {
    const config: OcxConfig = {
      port: 0,
      defaultProvider: "a",
      providers: {
        a: { adapter: "anthropic", baseUrl: "https://a.example.test/v1", reasoningEfforts: [] },
        b: { adapter: "anthropic", baseUrl: "https://b.example.test/v1", reasoningEfforts: ["low", "high"] },
      },
    };
    for (const [provider, expectedEffort] of [["a", undefined], ["b", "high"]] as const) {
      // Policy fallback parses each attempt from its preserved wire snapshot. Keep these
      // cases independent so the empty-ladder mutation cannot manufacture the capable result.
      const parsed = parseRequest({
        model: `${provider}/test-model`, input: [], reasoning: { effort: "high", summary: "auto" },
      });
      const route = routeModel(config, `${provider}/test-model`);
      await applyFinalRouteRequestNormalization({
        parsed, route, config,
        req: new Request("http://localhost/v1/responses"),
        logCtx: { model: parsed.modelId, provider },
        inboundWire: "chat",
      });
      expect(parsed.options.reasoning).toBe(expectedEffort);
      expect((parsed._rawBody as { reasoning: { effort?: string; summary: string } }).reasoning)
        .toEqual(expectedEffort ? { effort: expectedEffort, summary: "auto" } : { summary: "auto" });
      expect(parsed.options.hideThinkingSummary).toBeUndefined();
    }
  });
});
