import { afterEach, describe, expect, test } from "bun:test";
import {
  formatLowQuotaNotice,
  resolveLowQuotaDesktopSink,
  type LowQuotaNotice,
  type LowQuotaNotifierIo,
} from "../../src/codex/low-quota-desktop-notifier";

const NOTICE: LowQuotaNotice = { accountLabel: "pabc123", window: "weekly", percentUsed: 85, threshold: 80, resetAt: 1_800_000_000_000 };
const always = () => true;

type FakeChild = { argv: string[]; killed: number; exit(code: number | null): void; fail(error: Error): void };

function fakeSpawn(): { io: Pick<LowQuotaNotifierIo, "spawn">; children: FakeChild[] } {
  const children: FakeChild[] = [];
  return {
    children,
    io: {
      spawn(argv) {
        let resolveExit!: (code: number | null) => void;
        let rejectExit!: (error: Error) => void;
        const exited = new Promise<number | null>((resolve, reject) => { resolveExit = resolve; rejectExit = reject; });
        const child: FakeChild = { argv, killed: 0, exit: code => resolveExit(code), fail: error => rejectExit(error) };
        children.push(child);
        return { exited, kill: () => { child.killed++; } };
      },
    },
  };
}

const previousGuard = process.env.OCX_TEST_HOME_GUARD;
afterEach(() => {
  if (previousGuard === undefined) delete process.env.OCX_TEST_HOME_GUARD;
  else process.env.OCX_TEST_HOME_GUARD = previousGuard;
});

describe("low-quota desktop notifier", () => {
  test("formats a pseudonymous, bounded message with reset context", () => {
    expect(formatLowQuotaNotice(NOTICE, () => "Mon 10:00")).toEqual({
      title: "OpenCodex quota",
      body: "Account pabc123: weekly quota at 85% (threshold 80%), resets Mon 10:00.",
    });
    expect(formatLowQuotaNotice({ ...NOTICE, window: "short", resetAt: null }).body)
      .toBe("Account pabc123: 5-hour quota at 85% (threshold 80%), reset time unknown.");
  });

  test("unsupported platforms and missing notifiers resolve no sink", () => {
    const { io } = fakeSpawn();
    expect(resolveLowQuotaDesktopSink({ ...io, platform: "win32", exists: always })).toBeUndefined();
    expect(resolveLowQuotaDesktopSink({ ...io, platform: "linux", exists: () => false })).toBeUndefined();
    expect(resolveLowQuotaDesktopSink({ ...io, platform: "darwin", exists: () => false })).toBeUndefined();
  });
});
