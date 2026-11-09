import { afterEach, beforeEach, expect, test } from "bun:test";
import { handleResponses } from "../../src/server/responses/core";
import type { OcxConfig, OcxProviderConfig } from "../../src/types";
import { createTempHome, type TempHome } from "../helpers/temp-home";
import { acquireOwnedSpendHome } from "../helpers/owned-spend-home";

let home: TempHome;
let release: () => void;
beforeEach(() => { home = createTempHome(); release = acquireOwnedSpendHome(); });
afterEach(() => { release(); home.remove(); });

const tools = Array.from({ length: 151 }, (_, i) => ({ type: "function", name: `marker_${i}`, description: "Synthetic marker", parameters: { type: "object", properties: {} } }));
for (const stream of [false, true]) for (const combo of [false, true]) for (const tool of [false, true]) {
}

test("a 151-tool call/result continuation retains the catalog and result", async () => {
  const bodies: { messages: { role: string; content: unknown; tool_call_id?: string }[]; tools: unknown[]; tool_choice?: unknown }[] = [];
  const cfg = { defaultProvider: "primary", providers: { primary: {
    adapter: "openai-chat", baseUrl: "https://primary.invalid/v1", authMode: "key", apiKey: "fixture-key",
    fetch: (async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)));
      return Response.json({ choices: [{ message: bodies.length === 1
        ? { role: "assistant", tool_calls: [{ id: "call_cycle", type: "function", function: { name: "marker_150", arguments: "{}" } }] }
        : { role: "assistant", content: "result received" }, finish_reason: bodies.length === 1 ? "tool_calls" : "stop" }] });
    }) as typeof fetch,
  } } } as OcxConfig;
  const send = (input: unknown, choice: string) => handleResponses(new Request("http://localhost/v1/responses", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "primary/fixture", stream: false, store: false, input, tools, tool_choice: choice }),
  }), cfg, { model: "", provider: "" });
  const first = await (await send("Use a marker", "required")).json();
  expect(first.status).toBe("completed");
  const call = first.output.find((item: { type: string }) => item.type === "function_call");
  expect(call.name).toBe("marker_150");
  const second = await (await send([{ role: "user", content: "Use a marker" }, call, { type: "function_call_output", call_id: call.call_id, output: "synthetic marker result" }], "auto")).json();
  expect(second.status).toBe("completed");
  expect(JSON.stringify(second.output)).toContain("result received");
  expect(bodies).toHaveLength(2);
  expect(bodies[1]!.tools).toHaveLength(151);
  expect(bodies[1]!.messages).toContainEqual({ role: "tool", tool_call_id: call.call_id, content: "synthetic marker result" });
});

for (const call of ["remote.lookup", "lookup"]) test(`existing namespace echo restoration still satisfies required: ${call}`, async () => {
  const cfg = { defaultProvider: "primary", providers: { primary: {
    adapter: "openai-chat", baseUrl: "https://primary.invalid/v1", authMode: "key", apiKey: "fixture-key",
    fetch: (async () => Response.json({ choices: [{ message: { role: "assistant", tool_calls: [{ id: "call_alias", type: "function", function: { name: call, arguments: "{}" } }] }, finish_reason: "tool_calls" }] })) as typeof fetch,
  } } } as OcxConfig;
  const response = await handleResponses(new Request("http://localhost/v1/responses", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "primary/fixture", stream: false, store: false, input: "Use lookup", tool_choice: "required",
      tools: [{ type: "namespace", name: "remote", tools: [{ type: "function", name: "lookup", parameters: { type: "object", properties: {} } }] }] }),
  }), cfg, { model: "", provider: "" });
  const body = await response.json();
  expect(body.status).toBe("completed");
  expect(body.output.find((item: { type: string }) => item.type === "function_call")).toMatchObject({ name: "lookup", namespace: "remote" });
});

