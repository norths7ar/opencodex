import { expect, test } from "bun:test";
import { validatedCodexExecInvocation, validatedCodexExecInvocationAsync, type CodexExecBoundaryDeps } from "../../src/codex/exec-boundary";

for (const asyncMode of [false, true]) {
  test(`non-Darwin known-app spelling preserves passthrough, async=${asyncMode}`, async () => {
    const binary = "/Applications/Codex.app/Contents/Resources/codex";
    const deps: CodexExecBoundaryDeps = { platform: "linux" };
    const invocation = asyncMode ? await validatedCodexExecInvocationAsync(binary, [], deps)
      : validatedCodexExecInvocation(binary, [], deps);
    expect(invocation.file).toBe(binary);
  });
}
