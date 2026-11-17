import { expect, test } from "vitest";
import { buildQuotaSummary, formatQuotaPercent, quotaSeverity } from "../src/quota-summary";
import { freshQuotaReportsFromResponse } from "../src/provider-workspace/report";
const now = 1800000000000;
const summarize = (quota: unknown, updatedAt = now) =>
  buildQuotaSummary(
    freshQuotaReportsFromResponse(
      [{ provider: "chatgpt", label: "ChatGPT", updatedAt, quota }],
      now,
    ),
    (provider) => provider,
  );
test("weekly headline preserves the urgent short window in details", () => {
  const [row] = summarize({
    fiveHourPercent: 95,
    weeklyPercent: 31,
    weeklyResetAt: now + 3600000,
    updatedAt: now,
  });
  expect(row?.headline.percent).toBe(31);
  expect(row?.windows.find((window) => window.id === "quota.fiveHourLimit")?.severity).toBe(
    "critical",
  );
  expect(row?.headline.resetAt).toBe(now + 3600000);
});
test("expired measurements disappear and a fresh probe restores them", () => {
  expect(summarize(null)).toEqual([]);
  expect(summarize({ weeklyPercent: 50 }, now - 3600000)).toEqual([]);
  expect(summarize({ weeklyPercent: 0 })[0]?.headline.percent).toBe(0);
});
test("rounded labels agree with warning colors", () => {
  expect([formatQuotaPercent(69.6), quotaSeverity(69.6)]).toEqual(["69%", "normal"]);
  expect([formatQuotaPercent(89.6), quotaSeverity(89.6)]).toEqual(["89%", "warn"]);
  expect([formatQuotaPercent(90), quotaSeverity(90)]).toEqual(["90%", "critical"]);
});
