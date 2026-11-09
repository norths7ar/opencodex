import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defaultCodexHome } from "../../src/codex/home";
import { currentServiceHomes, serviceCodexHomeMatchesInstall } from "../../src/service";
import { removeTreeWithRetry } from "../helpers/remove-tree";
import { repoPath } from "../helpers/repo-root";

describe("default Codex home identity", () => {

  test.skipIf(process.platform !== "win32")("canonicalizes a junction-backed default Codex home", () => {
    const root = mkdtempSync(join(tmpdir(), "ocx-codex-home-junction-"));
    const target = join(root, "codex-target");
    const link = join(root, ".codex");
    try {
      mkdirSync(target);
      writeFileSync(join(target, "auth.json"), "{}\n", { encoding: "utf8" });
      const result = spawnSync("cmd.exe", ["/d", "/c", "mklink", "/J", link, target], {
        encoding: "utf8",
        windowsHide: true,
      });
      expect(result.status).toBe(0);
      expect(defaultCodexHome({ homedir: () => root, env: {} })).toBe(realpathSync(target));
    } finally {
      removeTreeWithRetry(root);
    }
  });
});
