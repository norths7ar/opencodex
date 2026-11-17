import { expect, test } from "vitest";
import { buildQuotaRows, quotaBarTone, formatResetFuture } from "../src/components/QuotaBars";
import type { TFn } from "../src/i18n/shared";
const t: TFn = ((key: string, vars?: Record<string, string | number>) =>
  vars ? `${key}:${Object.values(vars).join(",")}` : key) as TFn;
test("ChatGPT quota keeps zero usage and measured reset windows", () => {
  const rows = buildQuotaRows(
    { updatedAt: 1, fiveHourPercent: 0, weeklyPercent: 85, weeklyResetAt: 1800000000000 },
    "plus",
    t,
  );
  expect(rows.map((row) => [row.windowKey, row.percent])).toEqual([
    ["fiveHour", 0],
    ["weekly", 85],
  ]);
  expect(rows[1]?.resetAt).toBe(1800000000000);
  expect(buildQuotaRows(null, "plus", t)).toEqual([]);
});
test("monthly plans show their monthly window", () => {
  const rows = buildQuotaRows(
    { updatedAt: 1, shortPercent: 20, shortWindowSeconds: 18000, monthlyPercent: 60 },
    "go",
    t,
  );
  expect(rows.map((row) => [row.windowKey, row.percent])).toEqual([["monthly", 60]]);
});
test("bars distinguish normal usage, warning and exhaustion", () => {
  expect([quotaBarTone(50, 80), quotaBarTone(85, 80), quotaBarTone(100, 0)]).toEqual([
    "bar-green",
    "bar-warn",
    "bar-warn",
  ]);
});
test("reset countdown advances without inventing an unknown reset", () => {
  const now = 1800000000000;
  expect(formatResetFuture(now + 30 * 60000, t, "en", now)).toBe("quota.resetsRelativeMinutes:30");
  expect(formatResetFuture(now + 30 * 60000, t, "en", now + 10 * 60000)).toBe(
    "quota.resetsRelativeMinutes:20",
  );
  expect(formatResetFuture(undefined, t, "en", now)).toBe("");
});
