import { act, lazy, Suspense } from "react";
import { expect, test, vi } from "vitest";
import ErrorBoundary from "../src/components/ErrorBoundary";
import * as catalogs from "../src/i18n/catalogs";
import { en } from "../src/i18n/en";
import { LanguageProvider } from "../src/i18n/provider";
import { useI18n } from "../src/i18n/shared";
import { pageImport } from "../src/page-loader";
import { button, click, deferred, mount } from "./dom";

function LanguageControls() {
  const language = useI18n();
  return (
    <section>
      <output>{language.t("nav.models")}</output>
      <span>{language.locale}</span>
      {language.localeLoading && <span>Loading language</span>}
      {language.localeError && <span role="alert">Language unavailable</span>}
      <button onClick={() => language.setLocale("zh")}>Chinese</button>
      <button onClick={() => language.setLocale("fr")}>French</button>
    </section>
  );
}

test("a failed language download preserves the current interface and preference; selecting it again recovers", async () => {
  let response = deferred<catalogs.Catalog>();
  vi.spyOn(catalogs, "loadCatalog").mockImplementation(() => response.promise);
  const view = await mount(
    <LanguageProvider>
      <LanguageControls />
    </LanguageProvider>,
  );
  await click(button(view.host, "Chinese"));
  expect(view.host.textContent).toContain(en["nav.models"]);
  expect(view.host.textContent).toContain("Loading language");
  await act(async () => response.reject(new Error("chunk unavailable")));
  expect(view.host.querySelector('[role="alert"]')).not.toBeNull();
  expect(localStorage.getItem("ocx-lang")).toBe("en");
  expect(document.documentElement.lang).toBe("en");
  response = deferred<catalogs.Catalog>();
  await click(button(view.host, "Chinese"));
  await act(async () => response.resolve({ ...en, "nav.models": "模型" }));
  expect(view.host.querySelector("output")!.textContent).toBe("模型");
  expect(localStorage.getItem("ocx-lang")).toBe("zh");
  expect(view.host.querySelector('[role="alert"]')).toBeNull();
});

test("rapid language changes commit only the last selection even when the older download finishes last", async () => {
  const chinese = deferred<catalogs.Catalog>();
  const french = deferred<catalogs.Catalog>();
  vi.spyOn(catalogs, "loadCatalog").mockImplementation((locale) =>
    locale === "zh" ? chinese.promise : french.promise,
  );
  const view = await mount(
    <LanguageProvider>
      <LanguageControls />
    </LanguageProvider>,
  );
  await click(button(view.host, "Chinese"));
  await click(button(view.host, "French"));
  await act(async () => french.resolve({ ...en, "nav.models": "Modèles" }));
  await act(async () => chinese.resolve({ ...en, "nav.models": "模型" }));
  expect(view.host.querySelector("output")!.textContent).toBe("Modèles");
  expect(localStorage.getItem("ocx-lang")).toBe("fr");
  expect(document.documentElement.lang).toBe("fr");
});

test("a lazy page failure leaves navigation usable and requests a document reload; a fresh document can load the page", async () => {
  const module = deferred<{ default: () => React.JSX.Element }>();
  const Page = lazy(pageImport(() => module.promise));
  const reload = vi.spyOn(window.location, "reload").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
  const navigate = vi.fn();
  const props = {
    pageName: "Models",
    title: "Unavailable",
    message: "Try again",
    detailsLabel: "Details",
    reloadLabel: "Reload",
  };
  const view = await mount(
    <>
      <button onClick={navigate}>Home</button>
      <ErrorBoundary {...props}>
        <Suspense fallback={<p>Loading page</p>}>
          <Page />
        </Suspense>
      </ErrorBoundary>
    </>,
  );
  expect(view.host.textContent).toContain("Loading page");
  await click(button(view.host, "Home"));
  expect(navigate).toHaveBeenCalledOnce();
  await act(async () => module.reject(new Error("module 503")));
  expect(view.host.querySelector('[role="alert"]')!.textContent).toContain("module 503");
  await click(button(view.host, "Reload"));
  expect(reload).toHaveBeenCalledOnce();
  const Recovered = lazy(pageImport(async () => ({ default: () => <h1>Model catalog</h1> })));
  await view.render(
    <ErrorBoundary key="fresh-document" {...props}>
      <Suspense fallback="Loading">
        <Recovered />
      </Suspense>
    </ErrorBoundary>,
  );
  expect(view.host.textContent).toBe("Model catalog");
});
