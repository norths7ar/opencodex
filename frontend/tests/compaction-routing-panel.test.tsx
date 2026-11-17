/** @jsxImportSource react */
import { afterEach, beforeEach, expect, test } from "vitest";
import { Window } from "happy-dom";
import { act, StrictMode } from "react";
import type { Root } from "react-dom/client";
import { LanguageProvider } from "../src/i18n/provider";
import CompactionRoutingPanel from "../src/components/CompactionRoutingPanel";

const globals = [
  "document",
  "window",
  "navigator",
  "localStorage",
  "sessionStorage",
  "fetch",
  "HTMLElement",
  "IS_REACT_ACT_ENVIRONMENT",
] as const;
let previous: Record<string, PropertyDescriptor | undefined>;
let win: Window;
let root: Root | undefined;
let container: HTMLDivElement;
let setting: {
  model: string;
  reasoningEffort?: string;
  triggers?: string[];
  sourceModels?: string[];
} | null;
let failLoad: boolean;
let failSave: boolean;
let writes: unknown[];
const models = [
  { id: "cheap", provider: "gateway", namespaced: "gateway/cheap" },
  { id: "compact", provider: "gateway", namespaced: "gateway/compact" },
];
beforeEach(() => {
  previous = Object.fromEntries(
    globals.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]),
  );
  win = new Window({ url: "http://localhost/" });
  for (const key of [
    "document",
    "window",
    "navigator",
    "localStorage",
    "sessionStorage",
    "HTMLElement",
  ] as const) {
    Object.defineProperty(globalThis, key, {
      configurable: true,
      value: key === "window" ? win : win[key],
    });
  }
  Object.defineProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT", {
    configurable: true,
    value: true,
  });
  win.localStorage.setItem("ocx-lang", "en");
  setting = null;
  failLoad = false;
  failSave = false;
  writes = [];
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    writable: true,
    value: async (_input: unknown, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        writes.push(body);
        if (failSave) return Response.json({ error: "fixture failure" }, { status: 500 });
        setting = body.compactionRouting;
      } else if (failLoad) return Response.json({ error: "unavailable" }, { status: 503 });
      return Response.json({ compactionRouting: setting });
    },
  });
});

afterEach(async () => {
  if (root)
    await act(async () => {
      root!.unmount();
    });
  root = undefined;
  win.close();
  for (const key of globals) {
    if (previous[key]) Object.defineProperty(globalThis, key, previous[key]!);
    else delete (globalThis as Record<string, unknown>)[key];
  }
});

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
}
async function render(
  base = "",
  providers: Array<{ name: string; baseUrl: string }> = [],
  roster = models,
) {
  if (!root) {
    container = win.document.createElement("div") as unknown as HTMLDivElement;
    win.document.body.appendChild(container as never);
    root = (await import("react-dom/client")).createRoot(container);
  }
  await act(async () => {
    root!.render(
      <StrictMode>
        <LanguageProvider>
          <CompactionRoutingPanel apiBase={base} models={roster} providers={providers} />
        </LanguageProvider>
      </StrictMode>,
    );
  });
  await flush();
}
async function choose(id: string, label: string) {
  await act(async () => {
    container.querySelector<HTMLButtonElement>(`#compaction-routing-${id}`)!.click();
  });
  const option = [...win.document.querySelectorAll('[role="option"]')].find(
    (node) => node.textContent === label,
  );
  expect(option).toBeDefined();
  await act(async () => {
    (option as unknown as HTMLButtonElement).click();
  });
}
function saveButton() {
  return [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent === "Save",
  )!;
}
async function save() {
  await act(async () => {
    saveButton().click();
  });
}

test("saves model and optional effort, reloads, removes effort, and clears override", async () => {
  await render();
  expect(saveButton().disabled).toBe(true);
  await choose("model", "gateway/cheap");
  await choose("effort", "Low");
  await save();
  expect(writes).toEqual([
    { compactionRouting: { model: "gateway/cheap", reasoningEffort: "low" } },
  ]);
  expect(container.querySelector('[role="status"]')?.textContent).toBe(
    "Compaction settings saved.",
  );
  await render("/reloaded");
  expect(container.querySelector("#compaction-routing-effort")?.textContent).toContain("Low");
  await choose("effort", "Keep request effort");
  await save();
  expect(writes.at(-1)).toEqual({ compactionRouting: { model: "gateway/cheap" } });
  await choose("model", "Use conversation model");
  await save();
  expect(writes.at(-1)).toEqual({ compactionRouting: null });
  expect(container.querySelector<HTMLButtonElement>("#compaction-routing-effort")!.disabled).toBe(
    true,
  );
});

test("the trigger selection round-trips and discloses automatic compaction", async () => {
  await render();
  await choose("model", "gateway/cheap");
  expect(container.querySelector("#compaction-routing-triggers")?.textContent).toContain(
    "Manual /compact only",
  );
  expect(container.textContent).not.toContain("Automatic compaction runs on its own");
  await choose("triggers", "Manual and automatic");
  await save();
  expect(writes.at(-1)).toEqual({
    compactionRouting: { model: "gateway/cheap", triggers: ["manual", "auto"] },
  });
  expect(container.textContent).toContain("Automatic compaction runs on its own");
  await render("/reloaded");
  expect(container.querySelector("#compaction-routing-triggers")?.textContent).toContain(
    "Manual and automatic",
  );
  await choose("triggers", "Automatic only");
  await save();
  expect(writes.at(-1)).toEqual({
    compactionRouting: { model: "gateway/cheap", triggers: ["auto"] },
  });
  // Manual-only is sent as an omitted `triggers`, so the default save keeps the payload the
  // manual-only override already used.
  await choose("triggers", "Manual /compact only");
  await save();
  expect(writes.at(-1)).toEqual({ compactionRouting: { model: "gateway/cheap" } });
  await choose("model", "Use conversation model");
  expect(container.querySelector<HTMLButtonElement>("#compaction-routing-triggers")!.disabled).toBe(
    true,
  );
});

test("saving an effort change preserves the configured source-model boundary", async () => {
  setting = { model: "gateway/cheap", sourceModels: ["kimi/*", "google-antigravity/*"] };
  await render();
  await choose("effort", "Low");
  await save();
  expect(writes.at(-1)).toEqual({
    compactionRouting: {
      model: "gateway/cheap",
      reasoningEffort: "low",
      sourceModels: ["kimi/*", "google-antigravity/*"],
    },
  });
  expect(container.textContent).toContain("kimi/*");
});

function sourceCheckbox(label: string) {
  const row = [...container.querySelectorAll("label")].find((item) => item.textContent === label);
  return row?.querySelector("input");
}

test("the source scope checklist writes sourceModels and round-trips", async () => {
  await render();
  expect(container.querySelector<HTMLButtonElement>("#compaction-routing-sources")!.disabled).toBe(
    true,
  );
  await choose("model", "gateway/cheap");
  expect(container.querySelector("#compaction-routing-sources")?.textContent).toContain(
    "All conversation models",
  );
  expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  await choose("sources", "Selected sources only");
  // An empty selection would be rejected by the config schema, so saving stays disabled.
  expect(saveButton().disabled).toBe(true);
  expect(container.textContent).toContain("Select at least one source");
  await act(async () => {
    sourceCheckbox("gateway/*")!.click();
  });
  await act(async () => {
    sourceCheckbox("gateway/compact")!.click();
  });
  expect(saveButton().disabled).toBe(false);
  await save();
  expect(writes.at(-1)).toEqual({
    compactionRouting: { model: "gateway/cheap", sourceModels: ["gateway/*", "gateway/compact"] },
  });
  await render("/reloaded");
  expect(container.querySelector("#compaction-routing-sources")?.textContent).toContain(
    "Selected sources only",
  );
  expect(sourceCheckbox("gateway/*")!.checked).toBe(true);
  expect(sourceCheckbox("gateway/compact")!.checked).toBe(true);
  expect(sourceCheckbox("gateway/cheap")!.checked).toBe(false);
  await act(async () => {
    sourceCheckbox("gateway/compact")!.click();
  });
  await save();
  expect(writes.at(-1)).toEqual({
    compactionRouting: { model: "gateway/cheap", sourceModels: ["gateway/*"] },
  });
  await choose("sources", "All conversation models");
  await save();
  expect(writes.at(-1)).toEqual({ compactionRouting: { model: "gateway/cheap" } });
});

test("saved selectors outside the catalog stay visible and can be dropped deliberately", async () => {
  setting = { model: "gateway/cheap", sourceModels: ["kimi/*", "gateway/cheap"] };
  await render();
  expect(container.querySelector("#compaction-routing-sources")?.textContent).toContain(
    "Selected sources only",
  );
  expect(sourceCheckbox("kimi/*")!.checked).toBe(true);
  expect(sourceCheckbox("gateway/cheap")!.checked).toBe(true);
  expect(saveButton().disabled).toBe(true);
  await act(async () => {
    sourceCheckbox("kimi/*")!.click();
  });
  expect(saveButton().disabled).toBe(false);
  await save();
  expect(writes.at(-1)).toEqual({
    compactionRouting: { model: "gateway/cheap", sourceModels: ["gateway/cheap"] },
  });
});

test("the disclosure names the provider endpoint host when the provider is known", async () => {
  await render("", [
    { name: "gateway", baseUrl: "https://gw.example.com/v1" },
    { name: "openai-apikey", baseUrl: "https://api.openai.com/v1" },
  ]);
  await choose("model", "gateway/cheap");
  expect(container.querySelector('[role="note"]')?.textContent).toContain(
    "to gateway (gw.example.com) for summarization",
  );
  // A scoped override keeps the endpoint next to the sources it covers.
  await choose("sources", "Selected sources only");
  await act(async () => {
    sourceCheckbox("gateway/*")!.click();
  });
  expect(container.querySelector('[role="note"]')?.textContent).toContain(
    "source model matches gateway/* send the full conversation contents to gateway (gw.example.com)",
  );
});

test("failed save retains the draft and allows retry", async () => {
  await render();
  await choose("model", "gateway/cheap");
  failSave = true;
  await save();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Could not save");
  expect(container.querySelector("#compaction-routing-model")?.textContent).toContain(
    "gateway/cheap",
  );
  expect(saveButton().disabled).toBe(false);
  expect(setting).toBeNull();
  failSave = false;
  await save();
  expect(setting).toEqual({ model: "gateway/cheap" });
});

test("failed load disables editing and retry recovers", async () => {
  failLoad = true;
  await render();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Could not load");
  expect(container.querySelector("#compaction-routing-model")).toBeNull();
  failLoad = false;
  await act(async () => {
    [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Retry")!
      .click();
  });
  expect(container.querySelector<HTMLButtonElement>("#compaction-routing-model")!.disabled).toBe(
    false,
  );
});

test("retains a saved model missing from the current catalog", async () => {
  setting = { model: "gateway/retired", reasoningEffort: "high" };
  await render();
  expect(container.querySelector("#compaction-routing-model")?.textContent).toContain(
    "gateway/retired",
  );
  expect(saveButton().disabled).toBe(true);
});

test("filtering a large source catalog preserves hidden selections when saving", async () => {
  setting = { model: "gateway/cheap", sourceModels: ["gateway/model-399"] };
  const roster = Array.from({ length: 400 }, (_, index) => ({
    id: `model-${index}`,
    provider: "gateway",
    namespaced: `gateway/model-${index}`,
  }));
  await render("", [], roster);
  expect(sourceCheckbox("gateway/model-399")).toBeUndefined();
  const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  const setter = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(search, "model-399");
    search.dispatchEvent(new win.Event("input", { bubbles: true }) as unknown as Event);
  });
  expect(sourceCheckbox("gateway/model-399")?.checked).toBe(true);
  await act(async () => {
    sourceCheckbox("gateway/*")!.click();
  });
  await save();
  expect(setting?.sourceModels).toEqual(["gateway/model-399", "gateway/*"]);
});
