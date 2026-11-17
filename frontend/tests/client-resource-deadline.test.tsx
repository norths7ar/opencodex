import { act } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { clearClientResourceStoresForTests } from "../src/client-resource";
import { useDataSurface } from "../src/data-surface";
import { button, click, mount } from "./dom";
afterEach(clearClientResourceStoresForTests);
test("a stalled first load shows an error, and a manual retry restores the page", async () => {
  vi.useFakeTimers();
  let recover = false;
  function Page() {
    const resource = useDataSurface(
      "deadline-page",
      [],
      () => (recover ? Promise.resolve("Recovered") : new Promise<string>(() => {})),
      { isEmpty: () => false, deadlineMs: 100 },
    );
    return (
      <section>
        <output>{resource.state.showSkeleton ? "Loading" : resource.data}</output>
        {resource.state.showError && <span role="alert">Request timed out</span>}
        <button onClick={() => resource.refresh()}>Retry</button>
      </section>
    );
  }
  const view = await mount(<Page />);
  expect(view.host.textContent).toContain("Loading");
  await act(async () => vi.advanceTimersByTimeAsync(100));
  expect(view.host.querySelector('[role="alert"]')?.textContent).toBe("Request timed out");
  expect(view.host.textContent).not.toContain("Loading");
  recover = true;
  await click(button(view.host, "Retry"));
  expect(view.host.textContent).toContain("Recovered");
  expect(view.host.querySelector('[role="alert"]')).toBeNull();
});
test("a polling page recovers after a request stalls without requiring a reload", async () => {
  vi.useFakeTimers();
  let attempts = 0;
  function Page() {
    const resource = useDataSurface(
      "poll-timeout",
      [],
      () => (++attempts === 1 ? new Promise<string>(() => {}) : Promise.resolve("Online")),
      { isEmpty: () => false, deadlineMs: 100, pollMs: 200 },
    );
    return <output>{resource.state.showError ? "Offline" : (resource.data ?? "Loading")}</output>;
  }
  const view = await mount(<Page />);
  await act(async () => vi.advanceTimersByTimeAsync(100));
  expect(view.host.textContent).toBe("Offline");
  await act(async () => vi.advanceTimersByTimeAsync(100));
  expect(view.host.textContent).toBe("Online");
});
