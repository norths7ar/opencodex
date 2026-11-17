import { expect, test } from "vitest";
import { withFetchDeadline } from "../src/bounded-fetch";
test("a stalled usage-source request times out and a later attempt can complete", async () => {
  let observed!: AbortSignal;
  await expect(
    withFetchDeadline(10, undefined, async (signal) => {
      observed = signal;
      return new Promise<void>(() => {});
    }),
  ).rejects.toBeDefined();
  expect(observed.aborted).toBe(true);
  await expect(withFetchDeadline(1000, undefined, async () => "Saved")).resolves.toBe("Saved");
});
test("leaving the usage-source picker cancels its pending request before the deadline", async () => {
  const caller = new AbortController();
  let observed!: AbortSignal;
  const pending = withFetchDeadline(10000, caller.signal, async (signal) => {
    observed = signal;
    return new Promise<void>(() => {});
  });
  caller.abort();
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(observed.aborted).toBe(true);
});
