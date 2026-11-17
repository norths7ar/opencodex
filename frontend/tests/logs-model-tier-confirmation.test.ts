import { expect, test } from "vitest";
import { modelTitle } from "../src/pages/logs-model-title";
import type { TFn } from "../src/i18n/shared";
const t: TFn = ((key: string) => key.split(".").pop() ?? key) as TFn;
test("details distinguish routed identity, served model and requested versus echoed tier", () => {
  const title = modelTitle(
    {
      model: "channel/model",
      resolvedModel: "model",
      servedModel: "model-v2",
      requestedServiceTier: "priority",
      responseServiceTier: "default",
    },
    t,
  );
  expect(title).toContain("model=channel/model");
  expect(title).toContain("resolvedModel=model");
  expect(title).toContain("servedModel=model-v2");
  expect(title).toContain("requestedTier=priority");
  expect(title).toContain("responseTier=default");
  expect(title).not.toContain("confirmed");
});
