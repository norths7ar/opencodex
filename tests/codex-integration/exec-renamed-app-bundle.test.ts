import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { handleInternalCommand } from "../../src/cli/internal-command";
const root = "/Volumes/Apps/ChatGPT Beta.app";
const binary = `${root}/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`;
let logs: ReturnType<typeof spyOn<typeof console, "log">>;
let errors: ReturnType<typeof spyOn<typeof console, "error">>;
beforeEach(() => { logs = spyOn(console, "log").mockImplementation(() => {}); errors = spyOn(console, "error").mockImplementation(() => {}); });
afterEach(() => { logs.mockRestore(); errors.mockRestore(); });

test("ordinary executable helper calls retain passthrough", async () => {
  const target = "/opt/bin/codex";
  expect(await handleInternalCommand(["codex-exec-target", "--", target], {
    platform: "linux", executable: () => true, realpath: path => path,
  })).toBe(0);
  expect(logs.mock.calls).toEqual([[target]]);
});

test("recorded bundle mode refuses on a host without macOS verification", async () => {
  expect(await handleInternalCommand(["codex-exec-target", "--bundle-root", root, "--", binary], { platform: "linux" })).toBe(1);
  expect(logs.mock.calls).toEqual([]);
});
