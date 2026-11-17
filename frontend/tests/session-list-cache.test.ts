import { expect, test } from "vitest";
import { readSessionListCache, writeSessionListCache } from "../src/session-list-cache";
test("a corrupt browser list cache is ignored and replaced by the next successful load", () => {
  sessionStorage.setItem("model-list", "{broken");
  expect(readSessionListCache("model-list")).toBeNull();
  writeSessionListCache("model-list", [{ id: "fixture-model" }]);
  expect(readSessionListCache("model-list")).toEqual([{ id: "fixture-model" }]);
});
