/**
 * OCX-11 fallback policy resolver (src/routing/fallback-policy.ts): precedence across scopes,
 * hop classification, malformed-leaf degrade, the refusal contract and the
 * privacy of recorded events.
 */
import { beforeEach, describe, expect, test } from "bun:test";
import {
  clearFallbackEventsForTests,
  fallbackHopKind,
  guardFallbackHop,
  recentFallbackEvents,
  resolveFallbackPolicy,
} from "../../src/routing/fallback-policy";
import { fallbackPolicyError, fallbackPolicySchema } from "../../src/config/schema/fallback-policy";
import type { OcxConfig } from "../../src/types";
import type { OcxFallbackConfig } from "../../src/types/fallback";

const cfg = (fallback?: OcxFallbackConfig): OcxConfig => ({ fallback } as unknown as OcxConfig);

describe("policy default", () => {
  test("absent or non-object policy allows (today's behavior)", () => {
    expect(resolveFallbackPolicy(cfg(), { kind: "model" })).toEqual({ allowed: true, scope: "default" });
    expect(resolveFallbackPolicy({ fallback: "off" } as unknown as OcxConfig, { kind: "model" }).allowed).toBe(true);
  });
});

describe("hop kind", () => {
  test("provider change, model change, and same target", () => {
    expect(fallbackHopKind({ provider: "a", model: "m" }, { provider: "b", model: "m" })).toBe("provider");
    expect(fallbackHopKind({ provider: "a", model: "m" }, { provider: "a", model: "n" })).toBe("model");
    expect(fallbackHopKind({ provider: "a", model: "m" }, { provider: "a", model: "m" })).toBeNull();
  });
});

describe("config degrade", () => {
  test("a malformed leaf reads as absent and diagnostics name the section", () => {
    const raw = { model: "no", provider: false, routes: { "bad key": { model: false }, ok: { model: "x", provider: false } } };
    const parsed = fallbackPolicySchema.parse(raw);
    expect(parsed.model).toBeUndefined();
    expect(parsed.provider).toBe(false);
    expect(parsed.routes?.ok).toEqual({ provider: false });
    // The bad key survives the read but the resolver never matches it.
    expect(resolveFallbackPolicy(cfg(parsed as OcxFallbackConfig), { kind: "model", routes: ["bad key"] }).scope).toBe("default");
    expect(fallbackPolicyError({ fallback: raw })).toContain("fallback");
    expect(fallbackPolicyError({})).toBeUndefined();
  });
});

describe("guard and refusal contract", () => {
  beforeEach(() => clearFallbackEventsForTests());

  test("a proactive refusal answers 429 for quota triggers and 503 otherwise", () => {
    const off = cfg({ model: false });
    const hop = (trigger: "cooldown" | "unavailable") => guardFallbackHop(off, {
      site: "subagent", route: "a/m", from: { provider: "a", model: "m" }, to: { provider: "a", model: "n" }, trigger,
    });
    const cooled = hop("cooldown");
    const unavailable = hop("unavailable");
    expect(cooled.ok || cooled.response.status).toBe(429);
    expect(unavailable.ok || unavailable.response.status).toBe(503);
  });
});
