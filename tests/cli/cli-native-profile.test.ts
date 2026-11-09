import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { cmdAccount } from "../../src/cli/account";
import { apiError } from "../../src/cli/account-api";
import { nativeMainCodexLoginInvocation } from "../../src/cli/account-main";
import { removeTreeWithRetry } from "../helpers/remove-tree";

const originalLog = console.log;
const originalError = console.error;
const originalCodexHome = process.env.CODEX_HOME;
const tempRoots: string[] = [];

function tempConfigDir(prefix: string): string {
  const path = mkdtempSync(join(tmpdir(), prefix));
  tempRoots.push(path);
  return path;
}

afterEach(() => {
  console.log = originalLog;
  console.error = originalError;
  if (originalCodexHome === undefined) delete process.env.CODEX_HOME;
  else process.env.CODEX_HOME = originalCodexHome;
  while (tempRoots.length > 0) removeTreeWithRetry(tempRoots.pop()!);
});

describe("ocx account main", () => {
  test("cleanup guidance requires a literal true signal", () => {
    const errors: string[] = [];
    console.error = (...values: unknown[]) => errors.push(values.join(" "));

    expect(apiError({ error: "validation failed" }, "fallback", 500)).toBe(1);
    expect(apiError({ error: "validation failed", cleanupRequired: "true" }, "fallback", 500)).toBe(1);
    expect(errors).toEqual([
      "Error: validation failed",
      "Error: validation failed",
    ]);

    expect(apiError({ error: "validation failed", cleanupRequired: true }, "fallback", 500)).toBe(1);
    expect(errors.at(-1)).toBe("Warning: native-login staging cleanup is still required; run 'ocx account main doctor'.");
  });

  test("official login honors an explicit Windows runtime outside PATH through ComSpec", () => {
    const codexShim = "C:\\Portable Codex\\codex.cmd";
    const comSpec = "C:\\Windows\\System32\\cmd.exe";
    const env = {
      CODEX_CLI_PATH: codexShim,
      PATH: "",
      PATHEXT: ".CMD",
      ComSpec: comSpec,
    };
    const invocation = nativeMainCodexLoginInvocation("win32", {
      env,
      exists: path => path.toLowerCase() === codexShim.toLowerCase(),
      existsSync: path => path.toLowerCase() === codexShim.toLowerCase(),
      execFileSync: (file, args) => {
        expect(file).toBe(comSpec);
        expect(args.at(-1)).toContain("codex.cmd");
        return "codex-cli 9.9.9";
      },
      configDir: tempConfigDir("ocx-native-profile-explicit-runtime-"),
    });

    expect(invocation.file).toBe(comSpec);
    expect(invocation.args.slice(0, 3)).toEqual(["/d", "/s", "/c"]);
    expect(invocation.args[3]).toContain("codex.cmd");
    expect(invocation.args[3]).toContain('^"login^"');
    expect(invocation.options).toEqual({ windowsVerbatimArguments: true });
  });

  test("official login honors a persisted runtime outside PATH", () => {
    const runtime = "C:\\Portable Codex\\codex.exe";
    const configDir = tempConfigDir("ocx-native-profile-persisted-runtime-");
    writeFileSync(join(configDir, "codex-runtime.json"), `${JSON.stringify({
      version: 1,
      command: runtime,
      source: "configured",
      selectedVersion: "8.8.8",
      updatedAt: "2026-08-03T00:00:00.000Z",
    })}\n`);

    const invocation = nativeMainCodexLoginInvocation("win32", {
      env: { PATH: "", PATHEXT: ".EXE" },
      exists: path => path.toLowerCase() === runtime.toLowerCase(),
      existsSync: path => path.toLowerCase() === runtime.toLowerCase(),
      execFileSync: (file, args) => {
        expect(file).toBe(runtime);
        expect(args).toEqual(["--version"]);
        return "codex-cli 8.8.8";
      },
      configDir,
    });

    expect(invocation).toEqual({ file: runtime, args: ["login"], options: {} });
  });

  test("main reauth drives the dedicated native device namespace (#3898)", async () => {
    const requests: Array<{ method?: string; path: string }> = [];
    const output: string[] = [];
    const errors: string[] = [];
    console.log = (...values: unknown[]) => output.push(values.join(" "));
    console.error = (...values: unknown[]) => errors.push(values.join(" "));
    const fetchImpl: typeof fetch = async (input, init) => {
      const url = new URL(String(input));
      requests.push({ method: init?.method, path: url.pathname + url.search });
      if (url.pathname.endsWith("/main/reauth-device") && init?.method === "POST") {
        return Response.json({ flowId: "flow-1", status: "pending", verificationUrl: "https://auth.openai.com/codex/device", deviceCode: "ABCD-1234" });
      }
      if (url.pathname.endsWith("/main/reauth-device") && init?.method === "GET") {
        return Response.json({ flowId: "flow-1", status: "succeeded", credentialUpdated: true });
      }
      if (url.pathname.endsWith("/main/reauth-device") && init?.method === "DELETE") {
        return Response.json({ flowId: "flow-1", status: "cancelled" });
      }
      return Response.json({ error: "unexpected" }, { status: 500 });
    };
    const deps = { baseUrl: "http://127.0.0.1:10100", fetchImpl };

    expect(await cmdAccount(["main", "reauth", "--device", "--no-wait"], deps)).toBe(0);
    expect(requests[0]).toEqual({ method: "POST", path: "/api/codex-auth/main/reauth-device" });
    expect(output.join(" ")).toContain("ABCD-1234");
    expect(output.join(" ")).toContain("auth.openai.com/codex/device");
    expect(output.join(" ")).toContain("--flow flow-1");

    output.length = 0;
    expect(await cmdAccount(["main", "reauth", "--device", "--no-wait", "--json"], deps)).toBe(0);
    expect(JSON.parse(output.join("\n"))).toEqual({
      flowId: "flow-1",
      status: "pending",
      verificationUrl: "https://auth.openai.com/codex/device",
      deviceCode: "ABCD-1234",
    });

    expect(await cmdAccount(["main", "reauth", "status", "--flow", "flow-1"], deps)).toBe(0);
    expect(requests.at(-1)).toEqual({ method: "GET", path: "/api/codex-auth/main/reauth-device?flowId=flow-1" });
    expect(output.join(" ")).toContain("succeeded");

    expect(await cmdAccount(["main", "reauth", "cancel", "--flow", "flow-1"], deps)).toBe(0);
    expect(requests.at(-1)).toEqual({ method: "DELETE", path: "/api/codex-auth/main/reauth-device?flowId=flow-1" });
    expect(output.join(" ")).toContain("cancelled");

    expect(await cmdAccount(["main", "reauth", "--device", "extra"], deps)).toBe(1);
    expect(await cmdAccount(["main", "reauth"], deps)).toBe(1);
    // The pool login route is never touched for __main__.
    expect(JSON.stringify(requests)).not.toContain("/api/codex-auth/login");
    expect(errors.join(" ")).not.toContain("access_token");
  });
});
