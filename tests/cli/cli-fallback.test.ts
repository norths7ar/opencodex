/**
 * `ocx fallback` (OCX-11): human status lines, `ocx status --json` fallback when no proxy answers,
 * and usage refusals that never reach the proxy.
 */
import { describe, expect, test } from "bun:test";
import { fallbackStatusLines, handleFallbackCommand } from "../../src/cli/fallback";
import { collectStatusFallback } from "../../src/cli/status-fallback";
import type { OcxConfig } from "../../src/types";

describe("ocx fallback", () => {

  test("status --json reports the local policy when no proxy answers; a Child claims nothing locally", async () => {
    const config = { fallback: { model: false } } as OcxConfig;
    const local = await collectStatusFallback(config, { live: false, linkChild: false });
    expect(local).toMatchObject({ schema: "ocx.fallback/1", source: "local", global: { model: false, provider: true }, recent: [] });
    const child = await collectStatusFallback(config, { live: false, linkChild: true });
    expect(child).toMatchObject({ source: "local", global: { model: true, provider: true } });
  });

  test("usage errors exit 2 without a request", async () => {
    const calls: string[] = [];
    const deps = { findLiveProxy: async () => { calls.push("probe"); return null; } };
    for (const argv of [["bogus"], ["off", "--kind", "account"], ["off", "--route", "a", "--device", "b"], ["local", "maybe", "--device", "x"], ["local", "on"]]) {
      expect(await handleFallbackCommand(argv, deps as never)).toBe(2);
    }
    expect(calls).toEqual([]);
  });
});
