import { afterEach, beforeEach, expect, test } from "bun:test";
import { anthropicToResponsesTranslation } from "../../src/claude/inbound";
import { analyzeClaudeCompatibility } from "../../src/claude/compatibility";
import { handleClaudeMessages } from "../../src/server/claude-messages";
import type { OcxConfig } from "../../src/types";
import { acquireOwnedSpendHome } from "../helpers/owned-spend-home";
import { createTempHome, type TempHome } from "../helpers/temp-home";

const search = { type: "web_search_20250305", name: "web_search" };
const deferred = { name: "lookup", description: "Look up a record", defer_loading: true,
  input_schema: { type: "object", properties: { key: { type: "string" } }, required: ["key"] } };
const restrictions = [
  { allowed_domains: ["example.com"] },
  { blocked_domains: ["blocked.example"] },
  { user_location: { type: "approximate", country: "US", city: "Boston", timezone: "America/New_York" } },
  { allowed_domains: ["example.com"], blocked_domains: ["blocked.example"],
    user_location: { type: "approximate", country: "US" } },
];
const request = (tools: unknown[]) => ({ model: "combo/pick", max_tokens: 64, stream: false,
  tools, messages: [{ role: "user", content: "Use the available tools." }] });
let home: TempHome;
let release: () => void;
beforeEach(() => { home = createTempHome("ocx-translated-tool-restrictions-"); release = acquireOwnedSpendHome(); });
afterEach(() => { release(); home.remove(); });

test("translation never turns a restricted hosted search declaration into unrestricted search", () => {
  for (const restriction of restrictions) {
    // A duplicate unrestricted declaration must not bypass the restriction on the same tool.
    for (const tools of [[{ ...search, ...restriction }, deferred], [search, { ...search, ...restriction }, deferred]]) {
      const { body } = anthropicToResponsesTranslation(request(tools));
      expect(body.tools).toEqual([{ type: "function", name: "lookup", description: deferred.description,
        parameters: deferred.input_schema, strict: false }]);
    }
  }
});

for (const adapter of ["openai-responses", "openai-chat"] as const) {
  for (const [index, restriction] of [undefined, ...restrictions].entries()) {
  }
}

test("unrestricted search remains offered and deferred functions are included normally", () => {
  const raw = request([search, deferred]);
  const before = structuredClone(raw);
  const { body } = anthropicToResponsesTranslation(raw);
  expect(body.tools).toMatchObject([{ type: "web_search" }, { type: "function", name: "lookup", parameters: deferred.input_schema }]);
  expect(raw).toEqual(before);
});

test("forced restricted search refuses locally instead of naming an omitted hosted tool", () => {
  expect(() => anthropicToResponsesTranslation({ ...request([{ ...search, ...restrictions[0] }, deferred]),
    tool_choice: { type: "tool", name: "web_search" } })).toThrow("Restricted web search");
});

test("ordinary deferred functions can degrade to eager loading even under compatibility enforcement", () => {
  expect(analyzeClaudeCompatibility(request([deferred]), { mode: "enforce" }).decision).toBe("allow");
  // Unrepresentable top-level deferral and server-executed discovery retain their own admission.
  expect(analyzeClaudeCompatibility({ ...request([deferred]), defer_tools: true }, { mode: "enforce" }).decision).toBe("reject");
  expect(analyzeClaudeCompatibility(request([{ type: "tool_search_tool_regex_20251119", name: "tool_search" }, deferred]),
    { mode: "enforce" }).decision).toBe("reject");
});
