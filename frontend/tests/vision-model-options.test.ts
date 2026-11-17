import { expect, test } from "vitest";
import {
  visionModelOptions,
  visionSidecarBackendForModel,
  webSearchModelOptionsForPicker,
  webSearchSidecarSelectionForModel,
  type VisionModelOption,
} from "../src/pages/dashboard-shared";
const options: VisionModelOption[] = [
  { value: "channel/vision", label: "channel/vision", backend: "routed" },
];
test("server eligibility controls the picker and routed backend", () => {
  const rows = visionModelOptions(options, [], undefined);
  expect(rows).toEqual(options);
  expect(visionSidecarBackendForModel([], rows, "channel/vision")).toBe("routed");
  expect(visionModelOptions([], [], undefined)).toEqual([]);
});
test("catalog loss preserves the saved selection until discovery recovers", () => {
  const absent = visionModelOptions([], [], "channel/vision", "routed");
  expect(absent).toEqual(options);
  expect(visionSidecarBackendForModel([], absent, "channel/vision")).toBe("routed");
  expect(visionModelOptions(options, [], "channel/vision", "routed")).toEqual(options);
});
test("ChatGPT hosted-search auth slot keeps its native model identity", () => {
  const rows = webSearchModelOptionsForPicker(
    [
      {
        value: "gpt-5.6-luna",
        label: "gpt-5.6-luna",
        backend: "openai",
        model: "gpt-5.6-luna",
        authSlot: true,
      },
    ],
    [],
    undefined,
  );
  expect(webSearchSidecarSelectionForModel([], rows, "gpt-5.6-luna")).toEqual({
    backend: "openai",
    model: "gpt-5.6-luna",
  });
});
