// Carried from PR #6185 by yxr1995-maker and PR #6275 by codingbooo.
import { describe, expect, test } from "bun:test";

const row = (overrides: Partial<DiscoveryModelRow> & { id: string }): DiscoveryModelRow => ({
  provider: "gateway",
  ...overrides,
});

describe("decision model discovery", () => {
});
