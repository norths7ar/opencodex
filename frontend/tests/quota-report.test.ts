import { expect, test } from "vitest";
import { accountQuotaFromReport } from "../src/provider-workspace/report";
test("unavailable quota is not presented as unused quota", () => {
  expect(accountQuotaFromReport({ quota: null })).toBeNull();
  expect(accountQuotaFromReport({ quota: { updatedAt: 1 } })).toBeNull();
  expect(accountQuotaFromReport({ updatedAt: 1, quota: { fiveHourPercent: 0 } })).toEqual({
    fiveHourPercent: 0,
    updatedAt: 1,
  });
});
test("measured windows retain observation and reset timestamps", () => {
  expect(
    accountQuotaFromReport({
      updatedAt: 10,
      quota: { weeklyPercent: 40, weeklyResetAt: 1800000000000, updatedAt: 5 },
    }),
  ).toEqual({ weeklyPercent: 40, weeklyResetAt: 1800000000000, updatedAt: 5 });
});
