import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import * as serviceState from "../../src/service/state";
import { buildWindowsServiceScript } from "../../src/service";
import { windowsEnvIndirectBatchValue } from "../../src/lib/win-paths";
import { serviceApiTokenFilePath } from "../../src/lib/service-secrets";
import { removeTreeWithRetry } from "../helpers/remove-tree";

// Pinned service-home cases, split from service.test.ts to keep that file under its line cap.
const TEST_DIR = join(import.meta.dir, ".tmp-service-pinned-homes-test");
const previousOpenCodexHome = process.env.OPENCODEX_HOME;
const previousCodexHome = process.env.CODEX_HOME;
const previousApiAuthToken = process.env.OPENCODEX_API_AUTH_TOKEN;

afterEach(() => {
  if (previousOpenCodexHome === undefined) delete process.env.OPENCODEX_HOME;
  else process.env.OPENCODEX_HOME = previousOpenCodexHome;
  if (previousCodexHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = previousCodexHome;
  if (previousApiAuthToken === undefined) delete process.env.OPENCODEX_API_AUTH_TOKEN;
  else process.env.OPENCODEX_API_AUTH_TOKEN = previousApiAuthToken;
  if (existsSync(TEST_DIR)) removeTreeWithRetry(TEST_DIR);
});

function windowsBatchValue(value: string): string {
  return value
    .replace(/%/g, "%%")
    .replace(/"/g, "")
    .replace(/[\r\n]/g, "");
}

function pathVariants(path: string): string[] {
  const batchPath = windowsEnvIndirectBatchValue(path, windowsBatchValue);
  return [...new Set([
    path,
    path.replace(/\\/g, "\\\\"),
    batchPath,
    batchPath.replace(/\\/g, "\\\\"),
  ])];
}

function expectTextToContainPath(text: string, path: string): void {
  expect(pathVariants(path).some(candidate => text.includes(candidate))).toBe(true);
}

describe("systemd service unit", () => {



});

describe("Windows service task", () => {
  test("keeps install-time home paths literal when Task Scheduler changes its environment", () => {
    const oldCodexHome = process.env.CODEX_HOME;
    const oldOpenCodexHome = process.env.OPENCODEX_HOME;
    const oldUserProfile = process.env.USERPROFILE;
    const oldAppData = process.env.APPDATA;
    const oldLocalAppData = process.env.LOCALAPPDATA;
    try {
      process.env.CODEX_HOME = "C:\\Users\\한글사용자\\.codex";
      process.env.OPENCODEX_HOME = join(TEST_DIR, "install-opencodex-home-한글");
      process.env.USERPROFILE = "C:\\Users\\TaskUser";
      process.env.APPDATA = "C:\\Users\\TaskUser\\AppData\\Roaming";
      process.env.LOCALAPPDATA = "C:\\Users\\TaskUser\\AppData\\Local";
      const script = buildWindowsServiceScript();
      expect(script).toContain('set "CODEX_HOME=C:\\Users\\한글사용자\\.codex"');
      expect(script).toContain(`set "OPENCODEX_HOME=${join(TEST_DIR, "install-opencodex-home-한글")}"`);
      expect(script.indexOf("chcp 65001 >nul")).toBeLessThan(script.indexOf('set "CODEX_HOME='));
      expect(script).not.toContain('set "CODEX_HOME=%USERPROFILE%');
      expect(script).not.toContain('set "CODEX_HOME=%APPDATA%');
      expect(script).not.toContain('set "OPENCODEX_HOME=%USERPROFILE%');
      expect(script).not.toContain('set "OPENCODEX_HOME=%APPDATA%');
    } finally {
      if (oldCodexHome === undefined) delete process.env.CODEX_HOME;
      else process.env.CODEX_HOME = oldCodexHome;
      if (oldOpenCodexHome === undefined) delete process.env.OPENCODEX_HOME;
      else process.env.OPENCODEX_HOME = oldOpenCodexHome;
      if (oldUserProfile === undefined) delete process.env.USERPROFILE;
      else process.env.USERPROFILE = oldUserProfile;
      if (oldAppData === undefined) delete process.env.APPDATA;
      else process.env.APPDATA = oldAppData;
      if (oldLocalAppData === undefined) delete process.env.LOCALAPPDATA;
      else process.env.LOCALAPPDATA = oldLocalAppData;
    }
  });
});

describe("launchd service plist", () => {

});
