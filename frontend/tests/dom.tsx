import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach } from "vitest";

const roots: Root[] = [];
afterEach(async () => {
  await act(async () => roots.splice(0).forEach((root) => root.unmount()));
});

export async function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(node));
  return {
    host,
    render: async (next: ReactNode) => {
      await act(async () => root.render(next));
    },
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

export async function click(element: Element | null) {
  if (!(element instanceof HTMLElement)) throw new Error("Missing clickable element");
  await act(async () => element.click());
}

export function button(host: ParentNode, text: string) {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent === text);
  if (!found) throw new Error(`Missing button: ${text}`);
  return found;
}

export async function fill(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
