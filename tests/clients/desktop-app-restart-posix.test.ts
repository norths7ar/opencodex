import { afterEach, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { isUnderRoot } from "../../src/codex/desktop-app/types";
import {
  acquireDesktopRestartLock,
  releaseDesktopRestartLock,
  transferDesktopRestartLock,
} from "../../src/codex/desktop-app/lock";

function isolatedLock(): { lockPath: string } {
  return { lockPath: join(mkdtempSync(join(tmpdir(), "ocx-posix-restart-")), "lock") };
}

describe("desktop restart membership is a path boundary, not a prefix", () => {
  test("a sibling directory sharing the prefix is not a member", () => {
    expect(isUnderRoot("/usr/lib/chatgpt/ChatGPT", "/usr/lib/chatgpt")).toBe(true);
    expect(isUnderRoot("/usr/lib/chatgpt", "/usr/lib/chatgpt")).toBe(true);
    // The whole reason isUnderRoot exists: the same user can create these.
    expect(isUnderRoot("/usr/lib/chatgpt-evil/ChatGPT", "/usr/lib/chatgpt")).toBe(false);
    expect(isUnderRoot("/usr/lib/chatgpt-evil/ChatGPT", "/usr/lib/chatgpt")).toBe(false);
    expect(isUnderRoot("/usr/lib/chatgpt/ChatGPT", "/usr/lib/chatgpt")).toBe(true);
  });

  test("a forward slash separates on every host, a backslash only where the host says so", () => {
    // Windows accepts `/` wherever it accepts `\`, and a probe can return either. Reading a
    // forward-slash member as "outside the tree" is fail-closed but wrong: the restart the
    // user asked for silently becomes a no-op.
    expect(isUnderRoot("C:/Program Files/OpenAI.Codex/chatgpt.exe", "C:/Program Files/OpenAI.Codex")).toBe(true);
    expect(isUnderRoot("C:/Program Files/OpenAI.Codex-evil/chatgpt.exe", "C:/Program Files/OpenAI.Codex")).toBe(false);
    // The reverse is NOT symmetric. On POSIX a backslash is an ordinary filename
    // character, so admitting it as a separator would reopen the sibling hole.
    expect(isUnderRoot("/usr/lib/chatgpt\\evil", "/usr/lib/chatgpt")).toBe(process.platform === "win32");
  });
});

describe("the restart singleton lock", () => {
  const alive = new Set<number>([1001, 1002, 2001]);
  const io = (lockPath: string, pid: number) => ({
    lockPath, pid, isAlive: (p: number) => alive.has(p), now: () => 1_000_000,
  });

  test("a second caller is refused rather than queued", () => {
    // Two ladders at once are destructive, not merely wasteful: the second re-enumerates
    // during the first's relaunch and kills the app that was just started.
    const { lockPath } = isolatedLock();
    expect(acquireDesktopRestartLock(io(lockPath, 1001)).acquired).toBe(true);
    expect(acquireDesktopRestartLock(io(lockPath, 1002))).toEqual({ acquired: false, heldBy: 1001 });
  });

  test("the owner re-acquires its own lock, which is what lets a helper inherit one", () => {
    const { lockPath } = isolatedLock();
    acquireDesktopRestartLock(io(lockPath, 1001));
    expect(acquireDesktopRestartLock(io(lockPath, 1001)).acquired).toBe(true);
    expect(transferDesktopRestartLock(2001, io(lockPath, 1001))).toBe(true);
    expect(acquireDesktopRestartLock(io(lockPath, 2001)).acquired).toBe(true);
    expect(acquireDesktopRestartLock(io(lockPath, 1002))).toEqual({ acquired: false, heldBy: 2001 });
  });

  test("releasing a lock owned by somebody else is a no-op", () => {
    const { lockPath } = isolatedLock();
    acquireDesktopRestartLock(io(lockPath, 1001));
    releaseDesktopRestartLock(io(lockPath, 1002));
    expect(acquireDesktopRestartLock(io(lockPath, 1002))).toEqual({ acquired: false, heldBy: 1001 });
  });

  test("a lock whose owner is gone is reclaimed", () => {
    const { lockPath } = isolatedLock();
    acquireDesktopRestartLock(io(lockPath, 1001));
    alive.delete(1001);
    expect(acquireDesktopRestartLock(io(lockPath, 1002)).acquired).toBe(true);
    alive.add(1001);
  });

  test("a corrupt lock file does not wedge every future restart", () => {
    // Reachable whenever a writer dies between creating the file and writing it.
    const { lockPath } = isolatedLock();
    writeFileSync(lockPath, "{not json");
    expect(acquireDesktopRestartLock(io(lockPath, 1001)).acquired).toBe(true);
  });
});
