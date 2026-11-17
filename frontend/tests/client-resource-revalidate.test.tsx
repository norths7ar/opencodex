import { act, useMemo } from "react";
import { afterEach, expect, test } from "vitest";
import {
  clearClientResourceStoresForTests,
  invalidateClientResource,
} from "../src/client-resource";
import { useDataSurface } from "../src/data-surface";
import { readSessionListCache, writeSessionListCache } from "../src/session-list-cache";
import { deferred, mount } from "./dom";
afterEach(clearClientResourceStoresForTests);
test("returning to a cached list shows saved rows immediately and refreshes them from the server", async () => {
  writeSessionListCache("cached-models", ["Saved model"]);
  const response = deferred<string[]>();
  function Page() {
    const cached = useMemo(() => readSessionListCache<string[]>("cached-models"), []);
    const resource = useDataSurface("models-revisit", [], () => response.promise, {
      isEmpty: (rows) => !rows.length,
      initialData: cached ?? undefined,
    });
    return <output>{resource.state.showSkeleton ? "Loading" : resource.data?.join(",")}</output>;
  }
  const view = await mount(<Page />);
  expect(view.host.textContent).toBe("Saved model");
  await act(async () => response.resolve(["Current model"]));
  expect(view.host.textContent).toBe("Current model");
});
test("saving invalidates an in-flight list, and its old response cannot undo the updated result", async () => {
  const old = deferred<string>();
  let calls = 0;
  function Page() {
    const resource = useDataSurface(
      "saved-channel-list",
      [],
      () => (++calls === 1 ? old.promise : Promise.resolve("Updated channels")),
      { isEmpty: () => false },
    );
    return <output>{resource.data}</output>;
  }
  const view = await mount(<Page />);
  await act(async () => invalidateClientResource("saved-channel-list"));
  expect(view.host.textContent).toBe("Updated channels");
  await act(async () => old.resolve("Outdated channels"));
  expect(view.host.textContent).toBe("Updated channels");
});
