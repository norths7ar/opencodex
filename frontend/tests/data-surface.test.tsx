import { act } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { clearClientResourceStoresForTests } from "../src/client-resource";
import { useDataSurface } from "../src/data-surface";
import { deferred, mount } from "./dom";
afterEach(clearClientResourceStoresForTests);
test("a closed panel makes no request; opening it loads and distinguishes an empty result from loading", async () => {
  const response = deferred<string[]>();
  const fetcher = vi.fn(() => response.promise);
  function Panel({ open }: { open: boolean }) {
    const resource = useDataSurface("optional-panel", [], fetcher, {
      enabled: open,
      isEmpty: (rows) => rows.length === 0,
    });
    return (
      <output>
        {!open
          ? "Closed"
          : resource.state.showSkeleton
            ? "Loading"
            : resource.state.kind === "ready-empty"
              ? "No results"
              : resource.data?.join(",")}
      </output>
    );
  }
  const view = await mount(<Panel open={false} />);
  expect(fetcher).not.toHaveBeenCalled();
  await view.render(<Panel open />);
  expect(fetcher).toHaveBeenCalledOnce();
  expect(view.host.textContent).toBe("Loading");
  await act(async () => response.resolve([]));
  expect(view.host.textContent).toBe("No results");
});
