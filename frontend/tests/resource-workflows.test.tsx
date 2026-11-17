import { act } from "react";
import { afterEach, expect, test } from "vitest";
import { clearClientResourceStoresForTests } from "../src/client-resource";
import { useDataSurface } from "../src/data-surface";
import { button, click, deferred, mount } from "./dom";

afterEach(clearClientResourceStoresForTests);

test("leaving a slow page cancels its work; late results cannot replace the current page", async () => {
  const old = deferred<string>();
  const current = deferred<string>();
  const signals: AbortSignal[] = [];
  function Page({ name }: { name: string }) {
    const resource = useDataSurface(
      `page:${name}`,
      [name],
      (signal) => {
        signals.push(signal);
        return name === "old" ? old.promise : current.promise;
      },
      { isEmpty: () => false },
    );
    return (
      <section>
        <h1>{name}</h1>
        <output>{resource.state.showSkeleton ? "Loading" : resource.data}</output>
      </section>
    );
  }
  const view = await mount(<Page name="old" />);
  expect(view.host.textContent).toContain("Loading");
  await view.render(<Page name="current" />);
  expect(signals[0].aborted).toBe(true);
  await act(async () => current.resolve("Current result"));
  await act(async () => old.resolve("Obsolete result"));
  expect(view.host.textContent).toBe("currentCurrent result");
});

test("refresh keeps existing content, reports failure, and a retry replaces it without a cold reset", async () => {
  let response = deferred<string>();
  function Page() {
    const resource = useDataSurface("refresh-page", [], () => response.promise, {
      isEmpty: () => false,
    });
    return (
      <section>
        <output>{resource.state.showSkeleton ? "Loading" : resource.data}</output>
        {resource.state.refreshing && <span>Refreshing</span>}
        {resource.state.showError && <span role="alert">Refresh failed</span>}
        <button onClick={() => resource.refresh()}>Refresh</button>
      </section>
    );
  }
  const view = await mount(<Page />);
  await act(async () => response.resolve("Saved result"));
  response = deferred<string>();
  await click(button(view.host, "Refresh"));
  expect(view.host.textContent).toContain("Saved resultRefreshing");
  expect(view.host.textContent).not.toContain("Loading");
  await act(async () => response.reject(new Error("server restarting")));
  expect(view.host.querySelector('[role="alert"]')?.textContent).toBe("Refresh failed");
  expect(view.host.textContent).toContain("Saved result");
  response = deferred<string>();
  await click(button(view.host, "Refresh"));
  await act(async () => response.resolve("Recovered result"));
  expect(view.host.textContent).toContain("Recovered result");
  expect(view.host.querySelector('[role="alert"]')).toBeNull();
});
