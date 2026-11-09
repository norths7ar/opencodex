import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";

import { resolveKiroCliNativeSessionEntries } from "../../../src/oauth/kiro-credentials";

/**
 * Issue #710: `ocx login kiro` could not import an existing Windows Kiro CLI session because the
 * native-store candidate list only covered macOS and Linux. The reporter verified the real path is
 * `%LOCALAPPDATA%\Kiro-Cli\data.sqlite3` (and that pointing KIROCLI_DB_PATH there made the import
 * succeed), so the reader and token selector already worked — only discovery was short.
 *
 * These drive the pure resolver directly, so the win32 branch and each of its env fallback rungs are
 * exercised on any host: `process.platform` is stubbable in this repo, but `os.platform()` does not
 * follow it under Bun, so a platform-sniffing implementation could not be covered from macOS/Linux.
 */
describe("kiro-cli native session store resolution", () => {
  const WIN_HOME = "C:\\Users\\u";

  test("win32 uses %LOCALAPPDATA%\\Kiro-Cli\\data.sqlite3 (the path verified on issue #710)", () => {
    const entries = resolveKiroCliNativeSessionEntries({
      env: { LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local" },
      platform: "win32",
      home: WIN_HOME,
    });

    expect(entries).toEqual([
      { location: "kiro-cli-windows-data", path: "C:\\Users\\u\\AppData\\Local\\Kiro-Cli\\data.sqlite3" },
    ]);
  });

  // Middle rung of the win32 chain (LOCALAPPDATA -> USERPROFILE -> injected home). A blank
  // LOCALAPPDATA must fall THROUGH rather than produce a rootless `\AppData\Local\...` path.
  test("win32 falls back to %USERPROFILE%\\AppData\\Local when LOCALAPPDATA is blank", () => {
    const entries = resolveKiroCliNativeSessionEntries({
      env: { LOCALAPPDATA: "   ", USERPROFILE: "C:\\Users\\u" },
      platform: "win32",
      home: "D:\\injected-home",
    });

    expect(entries).toEqual([
      { location: "kiro-cli-windows-data", path: "C:\\Users\\u\\AppData\\Local\\Kiro-Cli\\data.sqlite3" },
    ]);
  });

  test("win32 falls back to the injected home when no Windows env var is usable", () => {
    for (const env of [{}, { LOCALAPPDATA: "" }, { LOCALAPPDATA: "", USERPROFILE: "" }]) {
      const entries = resolveKiroCliNativeSessionEntries({ env, platform: "win32", home: WIN_HOME });

      expect(entries).toEqual([
        { location: "kiro-cli-windows-data", path: "C:\\Users\\u\\AppData\\Local\\Kiro-Cli\\data.sqlite3" },
      ]);
    }
  });
});
