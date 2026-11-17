import { act } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { clearClientResourceStoresForTests } from "../src/client-resource";
import { useDataSurface } from "../src/data-surface";
import { mount } from "./dom";
afterEach(clearClientResourceStoresForTests);
test("a dashboard pauses while hidden, refreshes on return, and stops after navigation away", async () => {
  vi.useFakeTimers();
  let hidden = false;
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() =>
    hidden ? "hidden" : "visible",
  );
  let calls = 0;
  function Page() {
    const resource = useDataSurface("visible-page", [], async () => String(++calls), {
      isEmpty: () => false,
      pollMs: 100,
    });
    return <output>{resource.data}</output>;
  }
  const view = await mount(<Page />);
  expect(view.host.textContent).toBe("1");
  await act(async () => vi.advanceTimersByTimeAsync(100));
  expect(view.host.textContent).toBe("2");
  hidden = true;
  await act(async () => document.dispatchEvent(new Event("visibilitychange")));
  await act(async () => vi.advanceTimersByTimeAsync(500));
  expect(calls).toBe(2);
  hidden = false;
  await act(async () => document.dispatchEvent(new Event("visibilitychange")));
  expect(view.host.textContent).toBe("3");
  await view.render(null);
  await act(async () => vi.advanceTimersByTimeAsync(500));
  expect(calls).toBe(3);
});
