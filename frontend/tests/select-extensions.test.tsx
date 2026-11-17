/**
 * The `Select` extensions that let it replace every native `<select>` (form-controls-rule):
 * disabled options, type-ahead, unchanged re-pick semantics, `required`/`name` through a
 * proxy input, in-place menus inside a `<dialog>`, and the field sizes.
 */
import { afterEach, beforeEach, expect, test } from "vitest";
import { Window } from "happy-dom";
import { act, useState } from "react";
import type { Root } from "react-dom/client";
import { Select, type SelectOption } from "../src/ui";

const globals = ["document", "window", "navigator", "IS_REACT_ACT_ENVIRONMENT"] as const;
let previousGlobals: Record<(typeof globals)[number], unknown>;
let testWindow: Window;
let root: Root | undefined;

beforeEach(() => {
  previousGlobals = Object.fromEntries(
    globals.map((key) => [key, Reflect.get(globalThis, key)]),
  ) as typeof previousGlobals;
  testWindow = new Window({ url: "http://localhost/" });
  Object.defineProperties(globalThis, {
    document: { configurable: true, value: testWindow.document },
    window: { configurable: true, value: testWindow },
    navigator: { configurable: true, value: testWindow.navigator },
  });
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(async () => {
  if (root)
    await act(async () => {
      root!.unmount();
    });
  root = undefined;
  testWindow.close();
  for (const key of globals) {
    Object.defineProperty(globalThis, key, { configurable: true, value: previousGlobals[key] });
  }
});

const OPTIONS: SelectOption[] = [
  { value: "", label: "Choose", disabled: true },
  { value: "a", label: "Alpha" },
  { value: "b", label: "Bravo", disabled: true },
  { value: "c", label: "Charlie" },
  { value: "c2", label: "Cobalt" },
];

async function mount(node: React.ReactElement): Promise<HTMLElement> {
  const { createRoot } = await import("react-dom/client");
  const host = document.createElement("div");
  document.body.append(host);
  await act(async () => {
    root = createRoot(host);
    root.render(node);
  });
  return host;
}

function Harness({
  initial = "",
  onChange,
}: {
  initial?: string;
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <output>{value}</output>
      <Select
        value={value}
        options={OPTIONS}
        label="Pick"
        onChange={(next) => {
          onChange?.(next);
          setValue(next);
        }}
      />
    </>
  );
}

const trigger = (host: HTMLElement) => host.querySelector<HTMLButtonElement>("[role=combobox]")!;
const committed = (host: HTMLElement) => host.querySelector("output")!.textContent;
const activeText = (host: HTMLElement) =>
  document.getElementById(trigger(host).getAttribute("aria-activedescendant")!)?.textContent;
const key = (target: HTMLElement, name: string) =>
  act(async () => {
    target.dispatchEvent(
      new testWindow.KeyboardEvent("keydown", {
        key: name,
        bubbles: true,
      }) as unknown as KeyboardEvent,
    );
  });

test("disabled options are listed, announced and skipped by the keyboard", async () => {
  const host = await mount(<Harness initial="a" />);
  await key(trigger(host), "ArrowDown");
  const rows = [...document.querySelectorAll<HTMLButtonElement>("[role=option]")];
  expect(rows.map((row) => row.getAttribute("aria-disabled"))).toEqual([
    "true",
    null,
    "true",
    null,
    null,
  ]);
  expect(activeText(host)).toBe("Alpha");
  await key(trigger(host), "ArrowDown");
  expect(activeText(host)).toBe("Charlie"); // Bravo is skipped
  await key(trigger(host), "Home");
  expect(activeText(host)).toBe("Alpha"); // the disabled placeholder is not a stop
  await key(trigger(host), "ArrowUp");
  expect(activeText(host)).toBe("Alpha");
  const bravo = rows[2]!;
  await act(async () => {
    bravo.click();
  });
  expect(committed(host)).toBe("a");
});

test("type-ahead jumps to the next matching option and cycles on a repeated letter", async () => {
  const host = await mount(<Harness initial="a" />);
  await key(trigger(host), "c");
  expect(trigger(host).getAttribute("aria-expanded")).toBe("true");
  expect(activeText(host)).toBe("Charlie");
  await key(trigger(host), "c");
  expect(activeText(host)).toBe("Cobalt");
  await key(trigger(host), "Enter");
  expect(committed(host)).toBe("c2");
  // A letter with no enabled match (Bravo is disabled) changes nothing.
  await key(trigger(host), "b");
  expect(trigger(host).getAttribute("aria-expanded")).toBe("false");
});

test('re-picking the current value still reports it ("Custom…" entries rely on that)', async () => {
  const changes: string[] = [];
  const host = await mount(<Harness initial="a" onChange={(value) => changes.push(value)} />);
  await act(async () => {
    trigger(host).click();
  });
  await act(async () => {
    document.querySelector<HTMLButtonElement>('[role=option][data-value="a"]')!.click();
  });
  expect(changes).toEqual(["a"]);
  expect(trigger(host).getAttribute("aria-expanded")).toBe("false");
  await act(async () => {
    trigger(host).click();
  });
  await act(async () => {
    document.querySelector<HTMLButtonElement>('[role=option][data-value="c"]')!.click();
  });
  expect(changes).toEqual(["a", "c"]);
});

test("a selection inside a dialog remains reachable and commits the chosen value", async () => {
  let selected = "a";
  const host = await mount(
    <dialog open>
      <Select
        value="a"
        options={OPTIONS}
        label="Pick"
        onChange={(value) => {
          selected = value;
        }}
      />
    </dialog>,
  );
  await act(async () => {
    trigger(host).click();
  });
  const box = document.querySelector('[role="listbox"]')!;
  expect(host.querySelector("dialog")!.contains(box)).toBe(true);
  await act(async () => {
    box.querySelector<HTMLButtonElement>('[data-value="c"]')!.click();
  });
  expect(selected).toBe("c");
  expect(document.querySelector('[role="listbox"]')).toBeNull();
  expect(document.activeElement).toBe(trigger(host));
});
