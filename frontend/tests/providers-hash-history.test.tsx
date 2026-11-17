import { afterEach, beforeEach, expect, test, describe } from "vitest";
import { Window } from "happy-dom";
import { normalizeHashPath } from "../src/hash-routing";

describe("useAppRouteState (real hook)", () => {
  const globals = [
    "document",
    "window",
    "navigator",
    "localStorage",
    "IS_REACT_ACT_ENVIRONMENT",
  ] as const;
  let previous: Record<(typeof globals)[number], unknown>;
  let win: Window;
  let host: HTMLElement;
  let root: import("react-dom/client").Root | null = null;

  async function mountAt(hash: string, storage?: Storage) {
    win = new Window({ url: `http://localhost/${hash}` });
    Object.defineProperties(globalThis, {
      document: { configurable: true, value: win.document },
      window: { configurable: true, value: win },
      navigator: { configurable: true, value: win.navigator },
      localStorage: { configurable: true, value: storage ?? win.localStorage },
    });
    (
      globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }
    ).IS_REACT_ACT_ENVIRONMENT = true;
    host = win.document.createElement("div") as unknown as HTMLElement;
    win.document.body.appendChild(host as never);

    // Lazy imports: a static react-dom/client import binds to the document that existed
    // when the module graph loaded and corrupts sibling suites in the same process.
    const [{ act, useEffect }, { createRoot }, { useAppRouteState }] = await Promise.all([
      import("react"),
      import("react-dom/client"),
      import("../src/use-app-route-state"),
    ]);
    const seen: { current: ReturnType<typeof useAppRouteState> | null } = { current: null };
    function Probe() {
      const state = useAppRouteState();
      useEffect(() => {
        seen.current = state;
      }, [state]);
      return null;
    }
    await act(async () => {
      root = createRoot(host);
      root.render(<Probe />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    return { seen, act };
  }

  beforeEach(() => {
    previous = Object.fromEntries(
      globals.map((k) => [k, Reflect.get(globalThis, k)]),
    ) as typeof previous;
  });

  afterEach(async () => {
    if (root) {
      const current = root;
      const { act } = await import("react");
      await act(async () => {
        current.unmount();
      });
      root = null;
    }
    for (const key of globals) {
      Object.defineProperty(globalThis, key, { configurable: true, value: previous[key] });
    }
  });

  test("an unknown suffix is normalised through the hook", async () => {
    const { seen } = await mountAt("#models/nope");
    expect(normalizeHashPath(win.location.hash)).toBe("models");
    expect(seen.current!.page).toBe("models");
  });

  test("page navigation adds history and Back returns to the previous page", async () => {
    const { seen, act } = await mountAt("#providers");
    const before = win.history.length;
    await act(async () => {
      seen.current!.navigateToPage("models");
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(normalizeHashPath(win.location.hash)).toBe("models");
    expect(win.history.length).toBeGreaterThan(before);
    await act(async () => {
      win.history.back();
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(seen.current!.page).toBe("providers");
  });
});
