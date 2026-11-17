import { afterEach, beforeEach, vi } from "vitest";

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem("ocx-lang", "en");
  document.body.replaceChildren();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("Unexpected network request"))),
  );
  vi.spyOn(XMLHttpRequest.prototype, "send").mockImplementation(() => {
    throw new Error("Unexpected XMLHttpRequest");
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
