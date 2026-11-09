
/** What a low-quota notice may tell the operating system. Never an email, raw id or credential. */
export type LowQuotaNotice = {
  /** Pseudonymous `p<hex6>` log label of the pool account. */
  accountLabel: string;
  window: "short" | "weekly";
  percentUsed: number;
  threshold: number;
  /** Epoch milliseconds of the window reset, when known. */
  resetAt: number | null;
};

/** Resolves when the OS notifier accepted the notice; rejects on failure, timeout or abort. */
export type LowQuotaNoticeSink = (notice: LowQuotaNotice, signal: AbortSignal) => Promise<void>;

type SpawnedNotifier = { exited: Promise<number | null>; kill(): void };
export type LowQuotaNotifierIo = {
  platform?: NodeJS.Platform;
  /** Returns true when the absolute path is an existing executable candidate. */
  exists?: (path: string) => boolean;
  spawn?: (argv: string[]) => SpawnedNotifier;
  timeoutMs?: number;
  /** Formats the reset time for display; defaults to the host locale. */
  formatTime?: (epochMs: number) => string;
};

const TITLE = "OpenCodex quota";

export function formatLowQuotaNotice(notice: LowQuotaNotice, formatTime: (epochMs: number) => string = defaultFormatTime): { title: string; body: string } {
  const window = notice.window === "short" ? "5-hour" : "weekly";
  const reset = notice.resetAt === null ? "reset time unknown" : `resets ${formatTime(notice.resetAt)}`;
  return {
    title: TITLE,
    body: `Account ${notice.accountLabel}: ${window} quota at ${Math.round(notice.percentUsed)}% (threshold ${notice.threshold}%), ${reset}.`,
  };
}

function defaultFormatTime(epochMs: number): string {
  return new Date(epochMs).toLocaleString();
}

/** Windows keeps low-quota notices in the application log. */
export function resolveLowQuotaDesktopSink(io: LowQuotaNotifierIo = {}): LowQuotaNoticeSink | undefined {
  return undefined;
}
