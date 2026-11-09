import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { handledSignalExitCode } from "../../backend/src/lib/handled-signal-exit";
import { repoPath } from "../helpers/repo-root";

describe("handled signal exit code", () => {

  test("every other run keeps its clean exit", () => {
    expect(handledSignalExitCode("SIGTERM", { OCX_SERVICE: "1", OCX_SERVICE_MANAGED: "1" }, "linux")).toBe(0);
    expect(handledSignalExitCode("SIGTERM", { OCX_SERVICE: "1", OCX_SERVICE_MANAGED: "1" }, "win32")).toBe(0);
    // A companion or foreground proxy carries at most OCX_SERVICE=1.
    expect(handledSignalExitCode("SIGTERM", { OCX_SERVICE: "1" }, "linux")).toBe(0);
    expect(handledSignalExitCode("SIGTERM", {}, "linux")).toBe(0);
  });

  test("the proxy routes its signal shutdown through it", () => {
    const cli = readFileSync(repoPath("backend/src/cli/index.ts"), "utf8");
    expect(cli).toContain("process.exit(restored && shutdownSucceeded ? handledSignalExitCode(signal) : 1);");
  });
});
