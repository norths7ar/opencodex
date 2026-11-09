
import {
  listWindowsSnapshots,
  tokenizeCommandLine,
  type ProcessSnapshot,
} from "../app-server-processes";

export interface CodexWriterProcess {
  pid: number;
  commandLine: string;
}

export type CodexWriterProcessCheck =
  | { state: "ok"; processes: CodexWriterProcess[] }
  | { state: "unknown"; reason: "enumeration_failed" };

export interface CodexWriterProcessIo {
  platform?: NodeJS.Platform;
  getuid?: () => number | undefined;
  listSnapshots?: () => ProcessSnapshot[];
}

const TARGET_TRIPLE = /^[a-z0-9_]+-[a-z0-9_]+-[a-z0-9_]+(?:-[a-z0-9_]+)?$/i;

function basename(token: string): string {
  return token.replace(/\\/g, "/").split("/").pop()?.toLowerCase() ?? "";
}

function isOfficialCodexExecutable(token: string): boolean {
  const base = basename(token);
  if (base === "codex" || base === "codex.exe" || base === "codex.cmd") return true;
  const withoutSuffix = base.replace(/\.(?:exe|cmd)$/i, "");
  if (!withoutSuffix.startsWith("codex-")) return false;
  return TARGET_TRIPLE.test(withoutSuffix.slice("codex-".length));
}

function isCodeModeHostExecutable(token: string): boolean {
  const base = basename(token);
  return base === "codex-code-mode-host" || base === "codex-code-mode-host.exe";
}

function isInterpreterExecutable(token: string): boolean {
  const base = basename(token);
  return base === "node" || base === "node.exe"
    || base === "bun" || base === "bun.exe"
    || base === "deno" || base === "deno.exe";
}

function looksLikePathPrefix(token: string): boolean {
  return token.startsWith("/") || token.startsWith("./") || token.startsWith("../")
    || token.startsWith("~") || /^[a-z]:[\\/]/i.test(token);
}

function flattenedPathPrefix(
  commandLine: string,
  predicate: (candidate: string) => boolean,
): { executable: string; remainder: string } | null {
  const parts = commandLine.trim().split(/\s+/).filter(Boolean);
  for (let end = parts.length; end >= 1; end -= 1) {
    const executable = parts.slice(0, end).join(" ");
    if (!predicate(executable)) continue;
    return { executable, remainder: parts.slice(end).join(" ") };
  }
  return null;
}

function interpreterHostWithFlattenedPath(commandLine: string, tokens: readonly string[]): boolean {
  if (tokens.length < 2 || !isInterpreterExecutable(tokens[0]!)) return false;
  const trimmed = commandLine.trim();
  const firstWhitespace = trimmed.search(/\s/);
  if (firstWhitespace < 0) return false;
  const remainder = trimmed.slice(firstWhitespace).trim();
  return flattenedPathPrefix(remainder, isCodeModeHostExecutable) !== null;
}

/**
 * Match official Codex writer executables at argv0 plus the repository's
 * established interpreter-entrypoint form for code-mode-host.
 *
 * OS process listings can flatten argv into a display string. When an unquoted
 * executable path contains spaces, recover only a leading path-shaped executable
 * prefix; arbitrary later argv tokens still never turn a process into Codex.
 */
export function isCodexWriterCommandLine(commandLine: string, executable?: string): boolean {
  if (executable && (isOfficialCodexExecutable(executable) || isCodeModeHostExecutable(executable))) return true;
  const tokens = tokenizeCommandLine(commandLine.trim());
  if (tokens.length === 0) return false;
  if (isOfficialCodexExecutable(tokens[0]!) || isCodeModeHostExecutable(tokens[0]!)) return true;
  return tokens.length > 1
    && isInterpreterExecutable(tokens[0]!)
    && isCodeModeHostExecutable(tokens[1]!);
}

function effectiveUid(getuid?: () => number | undefined): number | undefined {
  try {
    return getuid ? getuid() : process.getuid?.();
  } catch {
    return undefined;
  }
}

function defaultSnapshots(platform: NodeJS.Platform, uid: number | undefined): ProcessSnapshot[] {
  if (platform === "win32") return listWindowsSnapshots();


  throw new Error("unsupported_process_enumeration_platform");
}

export function listRunningCodexProcesses(io: CodexWriterProcessIo = {}): CodexWriterProcessCheck {
  const platform = io.platform ?? process.platform;
  let snapshots: ProcessSnapshot[];
  try {
    snapshots = io.listSnapshots?.() ?? defaultSnapshots(platform, effectiveUid(io.getuid));
  } catch {
    return { state: "unknown", reason: "enumeration_failed" };
  }

  const byPid = new Map<number, CodexWriterProcess>();
  for (const snapshot of snapshots) {
    if (!Number.isSafeInteger(snapshot.pid) || snapshot.pid <= 0) continue;
    if (!isCodexWriterCommandLine(snapshot.commandLine, snapshot.executable)) continue;
    if (!byPid.has(snapshot.pid)) {
      byPid.set(snapshot.pid, { pid: snapshot.pid, commandLine: snapshot.commandLine });
    }
  }
  return {
    state: "ok",
    processes: [...byPid.values()].sort((a, b) => a.pid - b.pid),
  };
}
