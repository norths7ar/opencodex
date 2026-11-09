import { afterEach, describe, expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { runKeyringSmoke, type KeyringSmokeEntry } from "../../scripts/keyring-smoke";
import { stageStandaloneKeyringAddon } from "../../scripts/standalone-keyring";
import {
  keyringAssetForStandaloneTarget,
  inspectKeyringBinding,
  loadKeyringBinding,
  PACKAGED_DESKTOP_PRODUCT_NAME,
  packagedKeyringCandidates,
  type KeyringBinding,
} from "../../backend/src/lib/keyring-native";
import { repoPath } from "../helpers/repo-root";

const roots: string[] = [];

afterEach(() => {
  while (roots.length > 0) rmSync(roots.pop()!, { recursive: true, force: true });
});

function tempRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "ocx-keyring-package-"));
  roots.push(root);
  return root;
}

class MemoryKeyringEntry implements KeyringSmokeEntry {
  secret: Buffer | null = null;
  deletes = 0;

  async setSecret(secret: Uint8Array): Promise<void> {
    this.secret = Buffer.from(secret);
  }

  async getSecret(): Promise<Uint8Array | null> {
    return this.secret ? Buffer.from(this.secret) : null;
  }

  async deleteCredential(): Promise<boolean> {
    this.deletes += 1;
    this.secret?.fill(0);
    this.secret = null;
    return true;
  }
}

describe("runKeyringSmoke", () => {
  test("creates, exactly reads, and deletes a unique entry", async () => {
    const entry = new MemoryKeyringEntry();
    const created: Array<[string, string]> = [];
    const ids = ["service-id", "account-id"];

    await runKeyringSmoke({
      createEntry: async (service, account) => {
        created.push([service, account]);
        return entry;
      },
      createRandomBytes: (size) => Buffer.alloc(size, 0x5a),
      createId: () => ids.shift()!,
    });

    expect(created).toEqual([["opencodex.keyring-smoke.service-id", "ci-account-id"]]);
    expect(entry.deletes).toBe(1);
    expect(entry.secret).toBeNull();
  });

  test("deletes the entry when readback verification fails", async () => {
    const entry = new MemoryKeyringEntry();
    entry.getSecret = async () => Buffer.alloc(32, 0x00);

    await expect(runKeyringSmoke({
      createEntry: async () => entry,
      createRandomBytes: (size) => Buffer.alloc(size, 0x5a),
      createId: () => "test-id",
    })).rejects.toThrow("readback did not match");

    expect(entry.deletes).toBe(1);
    expect(entry.secret).toBeNull();
  });

  test("zeros the buffer returned by the keyring", async () => {
    const entry = new MemoryKeyringEntry();
    const readback = Buffer.alloc(32, 0x5a);
    const generated = Buffer.alloc(32, 0x5a);
    entry.getSecret = async () => readback;

    await runKeyringSmoke({
      createEntry: async () => entry,
      createRandomBytes: () => generated,
      createId: () => "test-id",
    });

    expect(readback.equals(Buffer.alloc(32))).toBe(true);
    expect(generated.equals(Buffer.alloc(32))).toBe(true);
  });

  test("preserves a readback error when deletion returns false", async () => {
    const entry = new MemoryKeyringEntry();
    const generated = Buffer.alloc(32, 0x5a);
    entry.getSecret = async () => Buffer.alloc(32, 0x00);
    entry.deleteCredential = async () => {
      entry.deletes += 1;
      return false;
    };

    const error = await runKeyringSmoke({
      createEntry: async () => entry,
      createRandomBytes: () => generated,
      createId: () => "test-id",
    }).catch(cause => cause);

    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain("readback did not match");
    expect((error as Error & { cleanupError?: unknown }).cleanupError).toBeInstanceOf(Error);
    expect(Object.keys(error as Error)).toContain("cleanupError");
    expect((error as Error & { cleanupError: Error }).cleanupError.message)
      .toContain("could not delete the temporary entry");
    expect(entry.deletes).toBe(1);
    expect(generated.equals(Buffer.alloc(32))).toBe(true);
  });

  test("preserves a readback error when deletion throws", async () => {
    const entry = new MemoryKeyringEntry();
    const deletionError = new Error("injected keyring deletion failure");
    entry.getSecret = async () => Buffer.alloc(32, 0x00);
    entry.deleteCredential = async () => {
      entry.deletes += 1;
      throw deletionError;
    };

    const error = await runKeyringSmoke({
      createEntry: async () => entry,
      createRandomBytes: (size) => Buffer.alloc(size, 0x5a),
      createId: () => "test-id",
    }).catch(cause => cause);

    expect((error as Error).message).toContain("readback did not match");
    expect((error as Error & { cleanupError?: unknown }).cleanupError).toBe(deletionError);
    expect(entry.deletes).toBe(1);
  });

  test("fails when deletion is the only failed operation", async () => {
    const entry = new MemoryKeyringEntry();
    entry.deleteCredential = async () => {
      entry.deletes += 1;
      return false;
    };

    await expect(runKeyringSmoke({
      createEntry: async () => entry,
      createRandomBytes: (size) => Buffer.alloc(size, 0x5a),
      createId: () => "test-id",
    })).rejects.toThrow("could not delete the temporary entry");

    expect(entry.deletes).toBe(1);
  });
});

describe("packaged keyring native binding", () => {
  test("maps every standalone release target to an exact native package", () => {
    expect(keyringAssetForStandaloneTarget("bun-windows-x64")?.filename).toBe("keyring.win32-x64-msvc.node");
    expect(keyringAssetForStandaloneTarget("bun-freebsd-x64")).toBeUndefined();
  });

  test("source installs use package resolution instead of probing beside Bun", () => {
    expect(packagedKeyringCandidates()).toEqual([]);
  });

  test("candidate paths follow the target platform's rules on any host", () => {
    expect(packagedKeyringCandidates({ root: "C:\\Program Files\\OpenCodex", platform: "win32", arch: "x64" }))
      .toEqual(["C:\\Program Files\\OpenCodex\\keyring\\keyring.win32-x64-msvc.node"]);
    expect(packagedKeyringCandidates({ root: "/opt/ocx", platform: "linux", arch: "x64" }))
      .toEqual([]);
  });

  test("loads only an existing deterministic packaged path and never consults cwd", () => {
    const binding = { Entry: class {}, AsyncEntry: class {} } as unknown as KeyringBinding;
    const calls: string[] = [];
    const result = loadKeyringBinding({
      candidates: ["/signed/app/keyring.node", "./keyring.node"],
      fileExists: path => path === "/signed/app/keyring.node",
      load: specifier => { calls.push(specifier); return binding; },
    });
    expect(result).toBe(binding);
    expect(calls).toEqual(["/signed/app/keyring.node"]);
  });

  test("falls back to package resolution for source and npm installs", () => {
    const calls: string[] = [];
    loadKeyringBinding({
      candidates: ["/missing/keyring.node"],
      fileExists: () => false,
      load: specifier => { calls.push(specifier); return { Entry: class {}, AsyncEntry: class {} }; },
    });
    expect(calls).toEqual(["@napi-rs/keyring"]);
  });

  test("the load-only probe verifies constructors without touching a credential", () => {
    expect(inspectKeyringBinding(() => ({ Entry: class {}, AsyncEntry: class {} }))).toEqual({
      schema: "ocx-keyring-load/1",
      available: true,
    });
    expect(() => inspectKeyringBinding(() => ({ Entry: class {}, AsyncEntry: null } as unknown as KeyringBinding)))
      .toThrow("does not export Entry and AsyncEntry");
  });

  test("stages the selected addon under the standalone output", () => {
    const root = tempRoot();
    const output = join(root, "dist", "standalone", "bun-windows-x64");
    const asset = keyringAssetForStandaloneTarget("bun-windows-x64")!;
    const wrapper = join(root, "node_modules", "@napi-rs", "keyring");
    const packageRoot = join(wrapper, "node_modules", asset.packageName);
    const source = join(packageRoot, asset.filename);
    mkdirSync(join(source, ".."), { recursive: true });
    writeFileSync(join(wrapper, "package.json"), JSON.stringify({ name: "@napi-rs/keyring", main: "index.js" }));
    writeFileSync(join(wrapper, "index.js"), "module.exports = {};");
    writeFileSync(join(packageRoot, "package.json"), JSON.stringify({ name: asset.packageName, main: asset.filename }));
    writeFileSync(source, "native-addon");
    const destination = stageStandaloneKeyringAddon(root, output, "bun-windows-x64");
    expect(destination).toBe(join(output, "keyring", asset.filename));
    expect(existsSync(destination)).toBe(true);
    expect(readFileSync(destination, "utf8")).toBe("native-addon");
  });

  test("stages an addon beside a symlinked wrapper in a virtual store", () => {
    const root = tempRoot();
    const output = join(root, "out");
    const asset = keyringAssetForStandaloneTarget("bun-windows-x64")!;
    const scope = join(root, "node_modules", "@napi-rs");
    const store = join(root, "node_modules", ".pnpm");
    const parent = join(store, "keyring-wrapper", "node_modules");
    const wrapper = join(parent, "@napi-rs", "keyring");
    const addon = join(store, "keyring-addon", "node_modules", asset.packageName);
    mkdirSync(scope, { recursive: true });
    mkdirSync(wrapper, { recursive: true });
    mkdirSync(addon, { recursive: true });
    writeFileSync(join(wrapper, "package.json"), JSON.stringify({ name: "@napi-rs/keyring", main: "index.js" }));
    writeFileSync(join(wrapper, "index.js"), "module.exports = {};");
    writeFileSync(join(addon, "package.json"), JSON.stringify({ name: asset.packageName, main: asset.filename }));
    writeFileSync(join(addon, asset.filename), "virtual-store-addon");
    symlinkSync(process.platform === "win32" ? wrapper : relative(scope, wrapper), join(scope, "keyring"), process.platform === "win32" ? "junction" : "dir");
    symlinkSync(process.platform === "win32" ? addon : relative(join(parent, "@napi-rs"), addon), join(parent, asset.packageName), process.platform === "win32" ? "junction" : "dir");

    const destination = stageStandaloneKeyringAddon(root, output, "bun-windows-x64");
    expect(readFileSync(destination, "utf8")).toBe("virtual-store-addon");
  });

  test("refuses a build whose target optional dependency was not installed", () => {
    const root = tempRoot();
    const wrapper = join(root, "node_modules", "@napi-rs", "keyring");
    mkdirSync(wrapper, { recursive: true });
    writeFileSync(join(wrapper, "package.json"), JSON.stringify({ name: "@napi-rs/keyring", main: "index.js" }));
    writeFileSync(join(wrapper, "index.js"), "module.exports = {};");
    const missing = join(root, "node_modules", "@napi-rs", "keyring-win32-x64-msvc");
    mkdirSync(missing, { recursive: true });
    writeFileSync(join(missing, "package.json"), JSON.stringify({ name: "@napi-rs/keyring-win32-x64-msvc", exports: "./missing.node" }));
    expect(() => stageStandaloneKeyringAddon(root, join(root, "out"), "bun-windows-x64"))
      .toThrow("install target optional dependencies");
  });

  test("desktop and release packaging retain the external addon and packaged-app proof", () => {
    const config = JSON.parse(readFileSync(repoPath("desktop", "src-tauri", "tauri.conf.json"), "utf8"));
    expect(config.productName).toBe(PACKAGED_DESKTOP_PRODUCT_NAME);
    expect(config.bundle.resources["resources/keyring"]).toBe("keyring");
    const release = readFileSync(repoPath(".github", "workflows", "release.yml"), "utf8");
    expect(release).toContain("ocx.exe,gui,keyring");
    expect(release).toContain("ocx gui keyring");
    expect(release).toContain("--os=${{ matrix.dependency_os }} --cpu=${{ matrix.dependency_cpu }}");
    const ci = readFileSync(repoPath(".github", "workflows", "ci.yml"), "utf8");
    expect(ci).toContain("backend/src/lib/standalone.ts");
    const cli = readFileSync(repoPath("backend/src", "cli", "index.ts"), "utf8");
    expect(cli).toContain('process.argv[2] === "__keyring-load-check"');
  });
});
