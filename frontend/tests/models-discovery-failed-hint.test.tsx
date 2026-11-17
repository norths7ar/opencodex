import { afterEach, beforeEach, expect, test } from "vitest";
import { Window } from "happy-dom";
import { act } from "react";
import type { Root } from "react-dom/client";
import { clearClientResourceStoresForTests } from "../src/client-resource";
import { LanguageProvider } from "../src/i18n/provider";
import Models from "../src/pages/Models";

/** Failed channel discovery keeps an actionable explanation beside catalog rows. */

let previousLanguage: unknown;
const originalFetch = globalThis.fetch;

beforeEach(() => {
  clearClientResourceStoresForTests();
  previousLanguage = (globalThis.navigator as { language?: unknown } | undefined)?.language;
  Object.defineProperty(globalThis.navigator, "language", { configurable: true, value: "en-US" });
});

afterEach(() => {
  clearClientResourceStoresForTests();
  globalThis.fetch = originalFetch;
  Object.defineProperty(globalThis.navigator, "language", {
    configurable: true,
    value: previousLanguage,
  });
});

const DOM_GLOBALS = [
  "document",
  "window",
  "localStorage",
  "sessionStorage",
  "IS_REACT_ACT_ENVIRONMENT",
  "setInterval",
  "clearInterval",
] as const;

const PROVIDER = "gemini-key-provider";
const MODEL_ID = "gemini-3.8-pro";

/**
 * Render the Models page against one provider whose rows exist, with the discovery state under
 * test, and return the group's rendered text.
 */
async function renderModelsPage(discovery: Record<string, unknown> | undefined): Promise<string> {
  const previousDescriptors = Object.fromEntries(
    DOM_GLOBALS.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]),
  ) as Record<(typeof DOM_GLOBALS)[number], PropertyDescriptor | undefined>;
  const testWindow = new Window({ url: "http://localhost/" });
  const container = testWindow.document.createElement("div");
  testWindow.document.body.append(container);
  let root: Root | undefined;
  // The page's poll registration must not fire during the assertion, and it reads whichever
  // setInterval is reachable, so both the window and the global are stubbed like the
  // neighbouring models-empty-provider harness does.
  Object.defineProperty(testWindow, "setInterval", { configurable: true, value: () => 1 });
  Object.defineProperties(globalThis, {
    document: { configurable: true, value: testWindow.document },
    window: { configurable: true, value: testWindow },
    localStorage: { configurable: true, value: testWindow.localStorage },
    sessionStorage: { configurable: true, value: testWindow.sessionStorage },
    IS_REACT_ACT_ENVIRONMENT: { configurable: true, value: true },
    setInterval: { configurable: true, value: () => 1 },
    clearInterval: { configurable: true, value: () => {} },
  });
  // Expanded, or the group body that carries the hint is never rendered.
  testWindow.localStorage.setItem("ocx-models-collapsed:v2", JSON.stringify([]));
  const rows = [
    { provider: PROVIDER, id: MODEL_ID, namespaced: `${PROVIDER}/${MODEL_ID}`, disabled: false },
  ];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/api/models")) return Response.json(rows);
    if (url.endsWith("/api/channels")) {
      return Response.json({
        revision: 1,
        channels: [
          {
            revision: 1,
            channel: {
              id: PROVIDER,
              name: PROVIDER,
              service_id: "custom",
              protocol: "openai-chat",
              auth_mode: "key",
              endpoints: { base_url: "https://upstream.invalid" },
              models: { live: true, static: [MODEL_ID] },
            },
            credential: { state: "configured" },
            has_custom_headers: false,
            discovery: {
              state:
                discovery?.status === "failed" ? "failed" : discovery ? "ready" : "not_checked",
              error:
                discovery?.status === "failed"
                  ? { code: "unavailable", message: "Discovery failed", retryable: true }
                  : null,
            },
          },
        ],
      });
    }
    if (url.endsWith("/api/selected-models")) {
      return Response.json({
        selected: { [PROVIDER]: [MODEL_ID] },
        available: { [PROVIDER]: [MODEL_ID] },
      });
    }
    if (url.endsWith("/api/provider-context-caps")) return Response.json({ caps: {} });
    if (url.endsWith("/api/shadow-call-settings"))
      return Response.json({ enabled: false, model: "" });
    return new Response(null, { status: 404 });
  }) as typeof fetch;

  try {
    const { createRoot } = await import("react-dom/client");
    await act(async () => {
      root = createRoot(container as unknown as HTMLElement);
      root.render(
        <LanguageProvider>
          <Models apiBase="http://localhost" reportRestart={() => {}} />
        </LanguageProvider>,
      );
    });
    await act(async () => {
      await new Promise((resolve) => testWindow.setTimeout(resolve, 0));
      await Promise.resolve();
    });
    return container.textContent ?? "";
  } finally {
    if (root) await act(async () => root?.unmount());
    container.remove();
    testWindow.close();
    for (const key of DOM_GLOBALS) {
      const descriptor = previousDescriptors[key];
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete (globalThis as Record<string, unknown>)[key];
    }
  }
}

test("a failed group WITH rows explains the discovery dependency, not just that it failed", async () => {
  const text = await renderModelsPage({ status: "failed", reason: "http", httpStatus: 401 });
  // The badge is the state before this change, and it stays.
  expect(text).toContain("Discovery failed");
  // The new part: name the mechanism and the exact control that changes it.
  expect(text).toContain("Model discovery is on for this provider");
  // Interpolated from pws.liveModels rather than restated, so the sentence can never name a
  // control whose label has moved on.
  expect(text).toContain("Discover models from provider");
  expect(text).toContain("Open provider settings");
});

test("a healthy group with rows gets no dependency hint", async () => {
  const text = await renderModelsPage({ status: "ok" });
  expect(text).toContain(MODEL_ID);
  expect(text).not.toContain("Model discovery is on for this provider");
  expect(text).not.toContain("Discovery failed");
});
