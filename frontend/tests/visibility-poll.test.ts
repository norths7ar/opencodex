import { expect, test, vi } from "vitest";
import { startVisibilityPoll } from "../src/visibility-poll";
test("a log poll pauses in the background, catches up on return, and cleans up when closed", async () => {
  vi.useFakeTimers();
  let hidden = false;
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() =>
    hidden ? "hidden" : "visible",
  );
  const refresh = vi.fn();
  const stop = startVisibilityPoll(refresh, 100);
  try {
    await vi.advanceTimersByTimeAsync(100);
    expect(refresh).toHaveBeenCalledTimes(1);
    hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(500);
    expect(refresh).toHaveBeenCalledTimes(1);
    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    expect(refresh).toHaveBeenCalledTimes(2);
    stop();
    await vi.advanceTimersByTimeAsync(500);
    expect(refresh).toHaveBeenCalledTimes(2);
  } finally {
    stop();
  }
});
