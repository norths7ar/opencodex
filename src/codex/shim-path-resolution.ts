import { accessSync, closeSync, constants, existsSync, fstatSync, lstatSync, openSync, readSync, realpathSync, statSync } from "node:fs";
import { posix, win32 } from "node:path";
import { SHIM_MARKER } from "./shim-templates";

export type CodexPathScanDeps = {
  pathValue?: string;
  /** Treat injected PATH entries as POSIX paths for portable path parsing. */
  posixPaths?: boolean;
  exists?: (path: string) => boolean;
  isShimFile?: (path: string) => boolean;
  isDirectory?: (path: string) => boolean;
  realpath?: (path: string) => string;
  executableEligible?: (path: string) => boolean;
};

export type CodexPathCandidate = {
  path: string;
  isShim: boolean;
};

export function realIsDirectory(path: string): boolean {
  try {
    return lstatSync(path).isDirectory();
  } catch {
    return true; // unreadable -> treat as unusable
  }
}

const SHIM_HEADER_MAX_BYTES = 16 * 1024;

/** Shared POSIX eligibility, following launcher symlinks without opening special files. */
export function isExecutableCodexCandidate(path: string): boolean {
  try {
    if (process.platform !== "win32") accessSync(path, constants.X_OK);
    return statSync(path).isFile();
  } catch { return false; }
}

export function inspectShimFile(path: string): boolean | null {
  let fd: number | undefined;
  try {
    if (process.platform !== "win32") {
      try { accessSync(path, constants.X_OK); } catch { return null; }
    }
    // Follow npm launcher symlinks, but never read a known special file.
    if (!statSync(path).isFile()) return null;
    const flags = constants.O_RDONLY | (process.platform === "win32" ? 0 : constants.O_NONBLOCK);
    fd = openSync(path, flags);
    // The entry can change after stat; a replacement FIFO must not block open or read.
    if (!fstatSync(fd).isFile()) return null;
    const header = Buffer.allocUnsafe(SHIM_HEADER_MAX_BYTES);
    let total = 0;
    while (total < header.length) {
      const count = readSync(fd, header, total, header.length - total, total);
      if (count === 0) break;
      total += count;
    }
    return header.subarray(0, total).toString("utf8").includes(SHIM_MARKER);
  } catch {
    // An unreadable command still shadows later PATH entries; do not claim they are active.
    return false;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function commandNames(name: string): string[] {
  if (process.platform !== "win32") return [name];
  const exts = (process.env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD;.PS1").split(";").filter(Boolean);
  return [name, ...exts.flatMap(ext => [`${name}${ext.toLowerCase()}`, `${name}${ext.toUpperCase()}`])];
}

/** The shell-local bin directory that fnm creates for one interactive shell. */
export function isFnmMultishellPath(path: string, posixPaths: boolean): boolean {
  const normalized = (posixPaths
    ? posix.normalize(path)
    : win32.normalize(path).replace(/\\/g, "/")).toLowerCase();
  return /(?:^|\/)fnm_multishells?(?:\/|$)/.test(normalized);
}

/** Find the command the shell would actually use, including an OpenCodex shim. */
export function findFirstCodexOnPath(deps: CodexPathScanDeps = {}): CodexPathCandidate | null {
  const exists = deps.exists ?? existsSync;
  const shimFile = deps.isShimFile ?? inspectShimFile;
  const isDir = deps.isDirectory ?? realIsDirectory;
  const usePosix = deps.posixPaths ?? (process.platform !== "win32");
  const joinPath = usePosix ? posix.join : win32.join;
  const pathSep = usePosix ? ":" : win32.delimiter;

  for (const dir of (deps.pathValue ?? process.env.PATH ?? "").split(pathSep).filter(Boolean)) {
    const names = commandNames("codex");
    for (const name of names) {
      const path = joinPath(dir, name);
      if (!exists(path) || isDir(path)) continue;
      const isShim = shimFile(path);
      if (isShim !== null) return { path, isShim };
    }
  }
  return null;
}

/**
 * Resolve a Codex command discovered through fnm's temporary PATH entry.
 *
 * A temporary path is safe to use only when its physical target is outside the
 * temporary tree. Returning null is deliberate: callers must not wrap the
 * shell-local path or silently choose a lower-priority Codex installation.
 */
export function resolveStableFnmCodexPath(
  path: string,
  posixPaths: boolean,
  realpath: ((path: string) => string) | undefined = realpathSync.native,
): string | null {
  if (!isFnmMultishellPath(path, posixPaths)) return path;
  try {
    // fnm normally links the temporary directory to the durable installation,
    // while npm may link the command file again into its package internals.
    // Resolve only the directory so the command basename remains the stable
    // installation entry that the installer is allowed to replace.
    const pathTools = posixPaths ? posix : win32;
    const resolvedParent = realpath(pathTools.dirname(path));
    if (!resolvedParent || isFnmMultishellPath(resolvedParent, posixPaths) || !pathTools.isAbsolute(resolvedParent)) return null;
    return pathTools.join(resolvedParent, pathTools.basename(path));
  } catch {
    return null;
  }
}
