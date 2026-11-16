const TERMINAL_SHORT_WINDOW_FRESHNESS_MS = 5 * 60_000;
const CODEX_EXHAUSTED_USAGE_PERCENT = 100;

/**
 * Above this a value is already milliseconds; at or below it, it is Unix seconds.
 *
 * Both reach storage, so the split has to live somewhere every reader can see. It lives on this
 * leaf rather than beside the merge that uses it because the dashboard reads the same stored
 * value and cannot import the disk-cache owner.
 */
const RESET_AT_SECONDS_MAX = 10_000_000_000;

/** Normalize a stored reset instant to milliseconds. */
export function resetAtToMs(resetAt: number): number {
  return resetAt < RESET_AT_SECONDS_MAX ? resetAt * 1000 : resetAt;
}

/** Only a valid reset at or before now proves a short-window reading has elapsed. */
export function hasElapsedShortWindow(resetAt: number | undefined, now: number): boolean {
  return (
    typeof resetAt === "number" &&
    Number.isFinite(resetAt) &&
    resetAt > 0 &&
    resetAtToMs(resetAt) <= now
  );
}

/** An exhausted short reading is live until its reset, or while fresh if no reset is known. */
export function isTerminalShortWindow(
  quota: { shortPercent?: number; shortResetAt?: number; shortObservedAt?: number },
  now: number,
): boolean {
  const finite = (value: unknown): value is number =>
    typeof value === "number" && Number.isFinite(value);
  if (!finite(quota.shortPercent) || quota.shortPercent < CODEX_EXHAUSTED_USAGE_PERCENT)
    return false;
  const resetAt = quota.shortResetAt;
  if (!finite(resetAt) || resetAt <= 0) {
    const observedAt = quota.shortObservedAt;
    if (!finite(observedAt)) return false;
    const age = now - observedAt;
    return age >= 0 && age <= TERMINAL_SHORT_WINDOW_FRESHNESS_MS;
  }
  return resetAtToMs(resetAt) > now;
}
