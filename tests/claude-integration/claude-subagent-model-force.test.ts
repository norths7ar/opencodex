import { expect, test } from "bun:test";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildClaudeEnv, buildNativeClaudeEnv } from "../../backend/src/cli/claude";
import { entryParts, resolveSubagentForceModel, withSubagentContextMarker } from "../../backend/src/claude/subagent-model";
import { inspectSubagentForceStatus, subagentForceSupport } from "../../backend/src/claude/subagent-force-status";
import { extractOcxRouteDirective } from "../../backend/src/claude/inbound-model-options";
import { configDiagnosticsFromRaw, validateConfigCandidate } from "../../backend/src/config/diagnostics";
import { removeTreeWithRetry } from "../helpers/remove-tree";
import type { OcxConfig } from "../../backend/src/types";

const config = (force?: string): OcxConfig => ({ port: 10100, providers: {}, defaultProvider: "openai", claudeCode: { ...(force === undefined ? {} : { subagentModelForce: force }) } });
const deps = { forceAvailable: ["combo/tev-auto", "gpt-6.1-sol", "mock/model"], authDetect: {
  readClaudeJson: () => undefined, credentialsFileExists: () => false, keychainProbe: () => "absent" as const,
} };
const MODEL = "CLAUDE_CODE_SUBAGENT_MODEL";
const FORCE = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE";

test("alias resolution reuses roster provider/native semantics and the long-context boundary", () => {
  for (const entry of deps.forceAvailable) {
    const c = config(entry);
    const { alias } = entryParts(entry, c);
    expect(resolveSubagentForceModel(c, { [alias]: 829_799 }, { entries: deps.forceAvailable })).toBe(alias);
    expect(resolveSubagentForceModel(c, { [alias]: 829_800 }, { entries: deps.forceAvailable })).toBe(`${alias}[1m]`);
    expect(resolveSubagentForceModel(c, { [alias]: 1000000 }, { entries: deps.forceAvailable })).toBe(`${alias}[1m]`);
  }
});

test("invalid hand edits degrade without losing providers; strict candidate validation rejects them", () => {
  const invalid = config("bad\nmodel");
  expect(validateConfigCandidate(invalid).ok).toBe(false);
  const diagnostics = configDiagnosticsFromRaw(JSON.stringify(invalid));
  expect(diagnostics.config.claudeCode?.subagentModelForce).toBeUndefined();
  expect(diagnostics.warnings.some(warning => warning.includes("subagentModelForce"))).toBe(true);
});

test("explicit wire model outranks legacy roster directive even without saved force", () => {
  const body = { model: "ocx-claude-combo--tev-auto", system: "<!-- ocx-route: ocx-claude-other--model -->" };
  expect(extractOcxRouteDirective(body)).toBe(body.model);
});

test("version boundary and read-only settings key presence are bounded and private", async () => {
  for (const version of [null, "bad"]) expect(subagentForceSupport(version)).toBe("unknown");
  expect(subagentForceSupport("2.1.256")).toBe("unsupported");
  for (const version of ["2.1.257", "2.2.0", "3.0.0"]) expect(subagentForceSupport(version)).toBe("supported");
  const dir = mkdtempSync(join(tmpdir(), "ocx-force-status-"));
  try {
    for (const key of [MODEL, FORCE]) {
      const text = JSON.stringify({ env: { [key]: "private-value" } });
      writeFileSync(join(dir, "settings.json"), text);
      const result = await inspectSubagentForceStatus(true, dir, async () => "2.1.257");
      expect(result).toMatchObject({ support: "supported", settingsOverride: true, settingsReadable: true });
      expect(JSON.stringify(result)).not.toContain("private-value");
      expect(readFileSync(join(dir, "settings.json"), "utf8")).toBe(text);
    }
    writeFileSync(join(dir, "settings.json"), "{");
    expect(await inspectSubagentForceStatus(false, dir, async () => null)).toMatchObject({ targetValid: false, support: "unknown", settingsReadable: false });
  } finally { removeTreeWithRetry(dir); }
});

test("legacy bare fallback stays intact and main turns have no route override", () => {
  for (const model of ["claude-sonnet-5", "unknown", "ocx-claude-invalid"]) {
    expect(extractOcxRouteDirective({ model, system: "<!-- ocx-route: ocx-claude-other--roster -->" }))
      .toBe("ocx-claude-other--roster");
  }
  for (const model of ["ocx-claude-mock--model", "claude-ocx-mock--model", "ocx-claude2-openrouter--vendor~smodel[1M]"]) {
    expect(extractOcxRouteDirective({ model, system: "<!-- ocx-route: other -->" })).toBe(model);
    expect(extractOcxRouteDirective({ model, system: "ordinary main turn" })).toBeNull();
  }
});

test("native Claude force has distinguishable identity and keeps authoritative context", () => {
  const c = config("anthropic/claude-sonnet-5");
  expect(resolveSubagentForceModel(c, { "claude-sonnet-5": 1000000 }, { entries: ["anthropic/claude-sonnet-5"] }))
    .toBe("ocx-claude-native--claude-sonnet-5[1m]");
  expect(resolveSubagentForceModel(c, {}, { selectors: ["claude-sonnet-5"] }))
    .toBe("ocx-claude-native--claude-sonnet-5");
});

test("fresh wire exposure is independent of windows and failed discovery cannot use stale windows", () => {
  const c = config("mock/model");
  expect(buildClaudeEnv(c, 10100, {}, {}, { ...deps, forceAvailable: undefined, forceAvailableSelectors: ["claude-ocx-mock--model[1m]"] })[MODEL])
    .toBe("ocx-claude-mock--model");
  const warnings: string[] = [];
  const env = buildClaudeEnv(c, 10100, {}, { "mock/model": 1000000 }, { ...deps, forceAvailable: undefined, warn: message => warnings.push(message) });
  expect(env[MODEL]).toBeUndefined(); expect(env[FORCE]).toBeUndefined(); expect(warnings).toHaveLength(1);
});


test("force rejects caller million markers without advertised identity or authoritative capacity", () => {
  for (const entry of ["mock/model[1m]", "gpt-6.1-sol[1M]", "anthropic/claude-sonnet-5[1m]"]) {
    const c = config(entry);
    const unmarked = entry.replace(/\[1m\]$/i, "");
    const alias = entryParts(unmarked, c).alias;
    expect(resolveSubagentForceModel(c, {}, { entries: [unmarked] })).toBeNull();
    expect(resolveSubagentForceModel(c, {}, { selectors: [alias] })).toBeNull();
    expect(resolveSubagentForceModel(c, { [alias]: 200000 }, { selectors: [alias] })).toBeNull();
    expect(resolveSubagentForceModel(c, { [alias]: Number.POSITIVE_INFINITY }, { selectors: [alias] })).toBeNull();
    expect(resolveSubagentForceModel(c, { [alias]: 1000000 }, { entries: [unmarked] })).not.toBeNull();
  }
});

test("force preserves exact advertised genuine marked models and legacy alias identity", () => {
  const c = config("kimi/k3[1m]");
  const marked = "ocx-claude-kimi--k3[1m]";
  expect(resolveSubagentForceModel(c, {}, { entries: ["kimi/k3[1m]"] })).toBe(marked);
  expect(resolveSubagentForceModel(c, {}, { selectors: ["claude-ocx-kimi--k3[1M]"] })).toBe(marked);
  expect(resolveSubagentForceModel(c, { [marked]: 1048576 }, { entries: ["kimi/k3[1m]"] })).toBe(marked);
  expect(resolveSubagentForceModel(c, { [marked]: 350000 }, { entries: ["kimi/k3[1m]"] })).toBe(marked);
  expect(resolveSubagentForceModel(config("mock/model[1m]"), { "ocx-claude-mock--model[1m]": 200000, "ocx-claude-mock--model": 1000000 }, { entries: ["mock/model"] })).toBeNull();
  expect(withSubagentContextMarker("ocx-claude-mock--legacy[1m]", {})).toBe("ocx-claude-mock--legacy[1m]");
});
