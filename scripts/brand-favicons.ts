#!/usr/bin/env bun
/**
 * Render the dashboard favicon from scripts/brand/opencodex-mark.svg.
 *
 * --check regenerates into a temporary directory and compares byte for byte, the same contract
 * desktop/scripts/generate-icons.ts has for the app icon set.
 */
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { render } from "./lib/icon-render";

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(repoRoot, "scripts", "brand", "opencodex-mark.svg");

/** Dashboard favicon, by path relative to the repository root. */
const FAVICON_SIZES: Record<string, number> = {
  "gui/public/favicon.png": 128,
};

/** Scratch files are flat, so a nested output name becomes one path segment. */
function scratchName(output: string): string {
  return output.replaceAll("/", "__");
}

function generateInto(target: string): string[] {
  const produced: string[] = [];
  for (const [output, size] of Object.entries(FAVICON_SIZES)) {
    render(size, join(target, scratchName(output)), source);
    produced.push(output);
  }

  return produced;
}

function main(): number {
  if (!existsSync(source)) {
    console.error(`[favicons] missing source: ${source}`);
    return 1;
  }
  const check = process.argv.includes("--check");
  const scratch = mkdtempSync(join(tmpdir(), "ocx-favicons-"));
  try {
    const produced = generateInto(scratch);
    if (!check) {
      // Written in one pass at the end, so a renderer failure part way through cannot leave one
      // site on the new mark and the other on the old one.
      for (const output of produced) {
        writeFileSync(join(repoRoot, output), readFileSync(join(scratch, scratchName(output))));
      }
      console.log(`[favicons] regenerated ${produced.length} favicons from ${basename(source)}`);
      return 0;
    }

    const drifted = produced.filter(output => {
      const committed = join(repoRoot, output);
      if (!existsSync(committed)) return true;
      return !readFileSync(join(scratch, scratchName(output))).equals(readFileSync(committed));
    });
    if (drifted.length > 0) {
      console.error(`[favicons] these do not match the app icon vector: ${drifted.join(", ")}`);
      console.error("[favicons] regenerate with: bun run favicons");
      return 1;
    }
    console.log(`[favicons] ${produced.length} favicons match the source`);
    return 0;
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

process.exit(main());

