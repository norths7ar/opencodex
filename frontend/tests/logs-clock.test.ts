import { expect, test } from "vitest";
import { logsClockAnchor, logsClockNow } from "../src/pages/logs-clock";
test("proxy-relative expiry is unaffected by a browser clock jump", () => {
  const anchor = logsClockAnchor(1800000000000, 500);
  expect(logsClockNow(anchor, 30500, 1800021600000)).toBe(1800000030000);
  expect(logsClockNow(anchor, 30500, 1799978400000)).toBe(1800000030000);
});
test("missing server time uses browser time until a valid response arrives", () => {
  expect(logsClockNow(logsClockAnchor(undefined, 10), 100, 42000)).toBe(42000);
  expect(logsClockNow(logsClockAnchor(50000, 100), 200, 42000)).toBe(50100);
});
