/**
 * Escaping of literal service homes in each generated service definition. Every case pins a
 * home that carries a character the target syntax would otherwise rewrite.
 */
import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as serviceState from "../../backend/src/service/state";
import { buildWindowsServiceScript, resolvedProxyEnv } from "../../backend/src/service";
import { buildServiceShellCommand, resolveServiceListenPort } from "../../backend/src/service/definition";

const previousOpenCodexHome = process.env.OPENCODEX_HOME;
beforeEach(() => {
  const sandbox = mkdtempSync(join(tmpdir(), "ocx-service-escaping-"));
  mkdirSync(join(sandbox, ".opencodex"), { recursive: true });
  process.env.OPENCODEX_HOME = join(sandbox, ".opencodex");
});
afterEach(() => {
  if (previousOpenCodexHome === undefined) delete process.env.OPENCODEX_HOME;
  else process.env.OPENCODEX_HOME = previousOpenCodexHome;
});

function withHomes<T>(codexHome: string, opencodexHome: string, body: () => T): T {
  const codex = spyOn(serviceState, "currentCodexHome").mockImplementation(() => codexHome);
  const windowsCodex = spyOn(serviceState, "currentWindowsServiceCodexHome").mockImplementation(() => codexHome);
  const opencodex = spyOn(serviceState, "currentOpenCodexHome").mockImplementation(() => opencodexHome);
  try {
    return body();
  } finally {
    codex.mockRestore();
    windowsCodex.mockRestore();
    opencodex.mockRestore();
  }
}

/**
 * Decode one double-quoted ExecStart= word the way systemd does: C-style escapes and
 * quotes at parse time, "%%" from specifier resolution, and "$$" from variable expansion
 * at exec time. Any other "%" or "$" would be rewritten by systemd, so it fails here.
 */
function decodeSystemdExecWord(word: string): string {
  const escapes: Record<string, string> = { n: "\n", r: "\r", t: "\t", "\\": "\\", '"': '"' };
  let out = "";
  for (let i = 0; i < word.length; i++) {
    const ch = word[i]!;
    const next = word[i + 1];
    if (ch === "\\") {
      if (next === undefined || !(next in escapes)) throw new Error("unsupported escape \\" + next);
      out += escapes[next];
      i++;
    } else if (ch === "%" || ch === "$") {
      if (next !== ch) throw new Error("systemd would expand " + ch + (next ?? ""));
      out += ch;
      i++;
    } else {
      out += ch;
    }
  }
  return out;
}

const PIN_END = "export CODEX_HOME OPENCODEX_HOME;";





/**
 * The values a plist parser returns. plutil keeps a literal CR, but a conforming XML 1.0
 * parser (Python's plistlib, for one) normalizes it to LF, so every value is checked with a
 * minimal XML reader that applies that line-end rule and the predefined and numeric entity
 * references, and also with plutil when the host has it.
 */
type PlistParser = "xml" | "plutil";
const PLIST_PARSERS: PlistParser[] = existsSync("/usr/bin/plutil") ? ["xml", "plutil"] : ["xml"];

function parsePlistStrings(plist: string, parser: PlistParser = "xml"): { env: Record<string, string>; args: string[] } {
  if (parser === "plutil") {
    const file = join(mkdtempSync(join(tmpdir(), "ocx-plist-parse-")), "job.plist");
    writeFileSync(file, plist, "utf8");
    const parsed = JSON.parse(execFileSync("/usr/bin/plutil", ["-convert", "json", "-o", "-", file], { encoding: "utf8" }));
    return { env: parsed.EnvironmentVariables, args: parsed.ProgramArguments };
  }
  const named: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
  const text = (raw: string): string => raw.replace(/\r\n?/g, "\n").replace(/&(#x[0-9a-fA-F]+|#\d+|\w+);/g, (_m, ref: string) =>
    ref.startsWith("#x") ? String.fromCodePoint(parseInt(ref.slice(2), 16))
      : ref.startsWith("#") ? String.fromCodePoint(Number(ref.slice(1)))
        : named[ref]!);
  const envBlock = /<key>EnvironmentVariables<\/key>\s*<dict>([\s\S]*?)<\/dict>/.exec(plist)?.[1] ?? "";
  const env = Object.fromEntries([...envBlock.matchAll(/<key>([^<]*)<\/key><string>([^<]*)<\/string>/g)].map(m => [text(m[1]!), text(m[2]!)]));
  const argBlock = /<key>ProgramArguments<\/key>\s*<array>([\s\S]*?)<\/array>/.exec(plist)?.[1] ?? "";
  return { env, args: [...argBlock.matchAll(/<string>([^<]*)<\/string>/g)].map(m => text(m[1]!)) };
}





describe("Windows wrapper escaping", () => {
  test("quoted set values keep ^ and ! literal and double %", () => {
    const home = "C:\\Users\\a^b\\100%!x\\.codex";
    const script = withHomes(home, "C:\\Users\\a^b\\.opencodex", () => buildWindowsServiceScript());
    // Inside set "NAME=value" cmd reads ^ literally, so a doubled caret names another directory.
    expect(script).toContain('set "CODEX_HOME=C:\\Users\\a^b\\100%%!x\\.codex"');
    expect(script).toContain('set "OPENCODEX_HOME=C:\\Users\\a^b\\.opencodex"');
    expect(script).not.toContain("a^^b");
    // "!" stays literal only while delayed expansion is off, before any assignment.
    expect(script.indexOf("setlocal EnableExtensions DisableDelayedExpansion")).toBeLessThan(script.indexOf('set "CODEX_HOME='));
  });

  test("the token file and log stay under the pinned OpenCodex home, not a profile variable", () => {
    // The task may run with another USERPROFILE than the installing shell. OPENCODEX_HOME is
    // written as a literal, so the token file must be too, or the two can name different profiles.
    const profile = join(tmpdir(), "ocx-task-profile");
    const previous = { profile: process.env.USERPROFILE, opencodex: process.env.OPENCODEX_HOME };
    process.env.USERPROFILE = profile;
    process.env.OPENCODEX_HOME = join(profile, ".opencodex");
    try {
      const script = withHomes(join(profile, ".codex"), join(profile, ".opencodex"), () => buildWindowsServiceScript());
      expect(script).toContain(`set "OPENCODEX_HOME=${join(profile, ".opencodex")}"`);
      expect(script).toContain(`set "OCX_API_TOKEN_FILE=${join(profile, ".opencodex", "service-api-token")}"`);
      expect(script).toContain(`set "OCX_SERVICE_LOG=${serviceState.serviceLogPath()}"`);
      expect(serviceState.serviceLogPath().startsWith(join(profile, ".opencodex"))).toBe(true);
      expect(script).not.toMatch(/set "(OCX_API_TOKEN_FILE|OCX_SERVICE_LOG|CODEX_SQLITE_HOME)=%/);
    } finally {
      if (previous.profile === undefined) delete process.env.USERPROFILE;
      else process.env.USERPROFILE = previous.profile;
      process.env.OPENCODEX_HOME = previous.opencodex;
    }
  });
});
