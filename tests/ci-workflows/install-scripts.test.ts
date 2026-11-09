import { describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { repoRoot as resolveRepoRoot } from "../helpers/repo-root";

// Windows CI runners spawn Node/Bun child processes slowly ("Slow filesystem detected");
// the package-main import test measured 9.4s there vs bun's 5s default. Same remedy as
// codex-history-provider / cursor-mcp-stdio.
setDefaultTimeout(30_000);

const root = pathToFileURL(resolveRepoRoot() + "/");
async function readText(path: string): Promise<string> {
  return await Bun.file(new URL(path, root)).text();
}

describe("install scripts", () => {
  test("npm package main is a Node-safe wrapper while Bun keeps the TypeScript API", async () => {
    const pkg = JSON.parse(await readText("package.json")) as {
      main?: string;
      exports?: { "."?: { bun?: string; default?: string } };
      dependencies?: Record<string, string>;
      testRunnerBun?: string;
      devDependencies?: Record<string, string>;
      scripts?: Record<string, string>;
      files?: string[];
    };

    expect(pkg.main).toBe("./bin/package-main.mjs");
    expect(pkg.exports?.["."]?.bun).toBe("./src/index.ts");
    expect(pkg.exports?.["."]?.default).toBe("./bin/package-main.mjs");
    // Bun 1.4.2 fixes the Windows fetch streaming regression reported in #6684; 1.4.4 keeps it
    // and ships NSS 3.131.1 roots, which drop the compromised eMudhra CAs (October 2026).
    // CI's testRunnerBun skipped 1.4.2: it segfaults while RE-LOADING the bunfig
    // preload that `--isolate` re-enters once per test file:
    // `load_preloads -> JSModuleLoader::loadModule -> JSPromise::status` dereferences a dead
    // promise and the process dies with "Segmentation fault at address 0x10". It is a crash in
    // the interpreter, not a test result, and no test content avoids it.
    //
    // The A/B is two Linux CI runs 40 minutes apart, either side of #4064 (02bc10e8af), which
    // is the commit that moved this line to 1.4.2:
    //   run 34274464811, 6bd3274ab2, Bun 1.4.0 -- 0 panics across all four shards
    //   run 34278407438, 02bc10e8af, Bun 1.4.2 -- 12 panics across all four shards
    // Every sampled 1.4.0 tree is clean and every sampled 1.4.2 tree crashes 12-14 times per
    // run. Linux hid it because `scripts/ci/run-bun-test-batches.sh` re-runs a crashed batch one
    // file per process, and one preload load cannot reach the second that faults, so the sweep
    // always "passes". Windows had no such sweep: shard 5/6 died on both attempts of runs
    // 35087572377, 35093667426 and 35098735960, always at the 67th file, and the file sitting at
    // that position changed between them.
    //
    // #4821 (2026-09-17) reverted #4064 (2026-09-09). #6713 run 37631558263 reproduced
    // the crash on Linux 1/4, macOS 2/2 and Windows 9/9; individual files pass.
    // Today's diagnostic sweep cannot turn a crashed primary batch green.
    //
    // testRunnerBun then moved from 1.4.0 to 1.4.3 for oven-sh/bun#44581 (#4956): 1.4.0's
    // Bun.spawnSync lost a child's exit once finalizers released polls on its private wait loop,
    // and spun past its own timeout. Scratch run 38213764818 stalled 8 of 252 Linux shard 1/4
    // batch-5 runs that way (CLI child a zombie, its pidfd armed but never polled).
    // tests/ci-workflows/test-runner-spawnsync-finalizer.test.ts keeps the runner above it.
    // The security transition now defaults both roles to 1.4.4. Keep their independent
    // validation; promotion still requires a green full cross-platform lane=all candidate run.
    expect(pkg.dependencies?.bun).toBe("1.4.4");
    expect(pkg.testRunnerBun).toBe("1.4.4");
    expect(pkg.dependencies?.zod).toBe("4.6.6");
    expect(pkg.devDependencies?.typescript).toBe("7.0.2");
    // Bun's type definitions track the runtime pin rather than a literal of their own.
    expect(pkg.devDependencies?.["@types/bun"]).toBe(pkg.dependencies?.bun);
    expect(pkg.scripts?.dev).toBe("bun run backend/src/cli/index.ts start");
    expect(pkg.scripts?.["dev:proxy"]).toBe("bun run backend/src/cli/index.ts start");
    expect(pkg.scripts?.["dev:gui"]).toBe("cd frontend && bun run dev");
  });

  test("Node can import the package main without executing the CLI", () => {
    const result = spawnSync("node", [
      "-e",
      "import('./backend/bin/package-main.mjs').then(m => { if (m.cliCommand !== 'ocx') process.exit(2); })",
    ], {
      cwd: repoRoot,
      encoding: "utf8",
    });

    expect(result.status).toBe(0);
  });

  test("npmignore keeps GUI development docs out of the package", async () => {
    const npmignore = await readText(".npmignore");
    const guiNpmignore = await readText("frontend/.npmignore");
    const guiReadme = await readText("frontend/README.md");

    expect(npmignore).toContain("frontend/README.md");
    expect(guiNpmignore).toContain("README.md");
    expect(guiReadme).toContain("opencodex dashboard");
    expect(guiReadme).toContain("bun run dev:proxy");
    expect(guiReadme).toContain("bun run dev:gui");
    expect(guiReadme).not.toContain("This template provides a minimal setup");
  });

});
