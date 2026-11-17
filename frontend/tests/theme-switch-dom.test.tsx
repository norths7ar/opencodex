import { afterEach, expect, test } from "vitest";
import { Window } from "happy-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ThemeSwitch, type ThemeMode } from "../src/components/theme-switch";
import { loadCatalog } from "../src/i18n/catalogs";
import { LanguageProvider } from "../src/i18n/provider";
import { getActiveLocale, setActiveLocale, type Locale } from "../src/i18n/shared";

const globals = [
  "document",
  "window",
  "navigator",
  "localStorage",
  "IS_REACT_ACT_ENVIRONMENT",
] as const;
const modes: ThemeMode[] = ["light", "dark", "system"];
let previous: Record<(typeof globals)[number], unknown>;
let previousLocale: Locale;
let root: Root | null = null;

function mount(theme: ThemeMode, onChange: (mode: ThemeMode) => void, locale: "en" | "ko" = "en") {
  previousLocale = getActiveLocale();
  const win = new Window({ url: "http://127.0.0.1:10100/" });
  previous = Object.fromEntries(
    globals.map((k) => [k, Reflect.get(globalThis, k)]),
  ) as typeof previous;
  win.localStorage.setItem("ocx-lang", locale);
  // Earlier DOM suites may leave non-writable globals; keep ours writable for later suites.
  Object.defineProperties(globalThis, {
    document: { configurable: true, writable: true, value: win.document },
    window: { configurable: true, writable: true, value: win },
    navigator: { configurable: true, writable: true, value: win.navigator },
    localStorage: { configurable: true, writable: true, value: win.localStorage },
    IS_REACT_ACT_ENVIRONMENT: { configurable: true, writable: true, value: true },
  });
  const host = win.document.createElement("div") as never as HTMLElement;
  win.document.body.appendChild(host as never);
  root = createRoot(host);
  const render = (mode: ThemeMode) =>
    act(() => {
      root!.render(
        <LanguageProvider>
          <ThemeSwitch theme={mode} onChange={onChange} />
        </LanguageProvider>,
      );
    });
  render(theme);
  return { host, render };
}

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = null;
  for (const k of globals)
    Object.defineProperty(globalThis, k, {
      configurable: true,
      writable: true,
      value: previous?.[k],
    });
  setActiveLocale(previousLocale);
});

for (const locale of ["en"] as const) {
  test(`the theme button names the current and next mode in ${locale}, with a decorative icon`, async () => {
    const dict = await loadCatalog(locale);
    const { host } = mount("system", () => {}, locale);
    // Design system v2: one button in the sidebar foot's orb row; a click steps to the next mode.
    const buttons = Array.from(host.querySelectorAll("button"));
    expect(buttons).toHaveLength(1);
    const button = buttons[0]!;
    expect(button.type).toBe("button");
    const label = dict["shellV2.themeCycle"]
      .replace("{mode}", dict["theme.system"])
      .replace("{next}", dict["theme.light"]);
    expect(button.getAttribute("aria-label")).toBe(label);
    expect(button.title).toBe(label);
    expect(button.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });
}

test("a click steps light, dark, system and back to light", () => {
  const calls: ThemeMode[] = [];
  const { host, render } = mount("light", (mode) => calls.push(mode));
  for (const mode of modes) {
    render(mode);
    act(() => {
      host.querySelector("button")!.click();
    });
  }
  expect(calls).toEqual(["dark", "system", "light"]);
});
