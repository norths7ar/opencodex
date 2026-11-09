import { expect, test } from "bun:test";
import { repoPath } from "../../helpers/repo-root";

test("each Kiro evidence module imports on its own", () => {
  for (const path of ["backend/src/providers/kiro-usage.ts", "backend/src/providers/kiro-account-state-disk.ts",
    "backend/src/providers/quota/account-cache.ts"]) {
    const result = Bun.spawnSync({ cmd: [process.execPath, "-e", `await import(${JSON.stringify(repoPath(path))})`] });
    expect(result.exitCode).toBe(0);
  }
});
