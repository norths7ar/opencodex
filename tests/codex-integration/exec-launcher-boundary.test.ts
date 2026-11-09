import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { EventEmitter } from "node:events";
import type { spawn } from "node:child_process";
import { spawnCodex, type CodexSpawnDeps } from "../../src/cli/codex-launcher";
import { handleInternalCommand } from "../../src/cli/internal-command";
import type { CodexExecBoundaryAsyncDeps } from "../../src/codex/exec-boundary";
import { maybeAutoRestoreCodexShim, skipsCodexShimAutoRestore } from "../../src/cli/codex-shim-autorestore";

const bundle = "/Applications/ChatGPT.app";
const binary = `${bundle}/Contents/Resources/codex`;
const alias = "/usr/local/bin/codex";
let logs: ReturnType<typeof spyOn<typeof console, "log">>;
let errors: ReturnType<typeof spyOn<typeof console, "error">>;
beforeEach(() => {
  logs = spyOn(console, "log").mockImplementation(() => {});
  errors = spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => { logs.mockRestore(); errors.mockRestore(); });

function harness() {
  const calls: Array<{ file: string; args: string[]; options: Record<string, unknown> }> = [];
  const boundary: CodexExecBoundaryAsyncDeps = {
    platform: "linux", executable: () => true,
    realpath: path => path === alias ? binary : path,
  };
  const deps: CodexSpawnDeps = {
    findOnPath: () => "/owned/codex-wrapper",
    inspectShim: () => ({ status: "matched", selectedRole: "wrapper", backingPath: alias }) as ReturnType<NonNullable<CodexSpawnDeps["inspectShim"]>>,
    execBoundary: boundary,
    spawn: ((file: string, args: string[], options: Record<string, unknown>) => {
      calls.push({ file, args, options });
      const child = new EventEmitter();
      queueMicrotask(() => child.emit("exit", 7, null));
      return child;
    }) as typeof spawn,
  };
  return { calls, boundary, deps };
}

test("non-bundle targets preserve arguments and do not invoke bundle verification", async () => {
  const h = harness();
  h.deps.findOnPath = () => "/usr/bin/codex";
  h.deps.inspectShim = () => ({ status: "absent" }) as ReturnType<NonNullable<CodexSpawnDeps["inspectShim"]>>;
  expect(await spawnCodex(["exec", "argument with spaces"], {}, h.deps)).toBe(7);
  expect(h.calls[0].file).toBe("/usr/bin/codex");
  expect(h.calls[0].args).toEqual(["exec", "argument with spaces"]);
});

test("Windows CMD invocation keeps its interpreter options and launch environment", async () => {
  const h = harness();
  const command = "C:/test/codex.cmd";
  h.deps.findOnPath = () => command;
  h.deps.inspectShim = () => ({ status: "absent" }) as ReturnType<NonNullable<CodexSpawnDeps["inspectShim"]>>;
  h.deps.execBoundary = { platform: "win32", exists: () => true };
  const env = { ComSpec: "C:/Windows/System32/cmd.exe", PATH: "", PATHEXT: ".CMD;.EXE" };
  expect(await spawnCodex(["--version"], env, h.deps)).toBe(7);
  expect(h.calls[0].file).toBe(env.ComSpec);
  expect(h.calls[0].args.slice(0, 3)).toEqual(["/d", "/s", "/c"]);
  expect(h.calls[0].options.windowsVerbatimArguments).toBe(true);
});

test("hidden validator rejects malformed argv and line-oriented path ambiguity", async () => {
  for (const args of [[], ["--"], ["--", ""], ["--", "a\nb"], ["--", "a\rb"], ["--", alias, "extra"]]) {
    expect(await handleInternalCommand(["codex-exec-target", ...args])).toBe(2);
  }
  expect(logs.mock.calls).toHaveLength(0);
});

test("validator admission skips root shim repair even for malformed arguments", () => {
  let reads = 0, restores = 0;
  for (const suffix of [[], ["--", alias], ["--", alias, "extra"]]) {
    maybeAutoRestoreCodexShim("internal", ["internal", "codex-exec-target", ...suffix], {
      env: {}, warn: () => { throw new Error("unexpected repair warning"); },
      readConfig: () => { reads++; throw new Error("unexpected configuration read"); },
      restore: () => { restores++; throw new Error("unexpected shim repair"); },
    });
  }
  expect(reads).toBe(0); expect(restores).toBe(0);
  expect(skipsCodexShimAutoRestore("internal", ["internal", "chatgpt-app-server-filter"])).toBe(false);
});
