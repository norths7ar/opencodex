import { codexExecInvocation } from "./exec-invocation";
import type { SpawnInvocation } from "../lib/win-exec";

export class CodexExecTrustError extends Error {
  readonly reason: string;
  readonly code = "CODEX_EXEC_UNTRUSTED";
  constructor(reason: string) {
    super("untrusted Codex exec target: " + reason);
    this.name = "CodexExecTrustError";
    this.reason = reason;
  }
}

export interface CodexExecBoundaryDeps {
  /** Spawn environment; PATH resolution uses exactly this list. */
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  /** Working directory the spawn will use; empty/relative PATH segments resolve against it. */
  cwd?: string;
  /** Existence probe: win32 passthrough PATH resolution and POSIX EACCES classification. */
  exists?: (path: string) => boolean;
  /** POSIX spawnability predicate: regular file + X_OK. */
  executable?: (path: string) => boolean;
  /** Canonicalizer; returns null when the path cannot be resolved. */
  realpath?: (path: string) => string | null;
  /** CLI handoffs verify any enclosing app, without trusting mutable bundle identity metadata. */
  verifyAnyAppBundle?: boolean;
  /** Discovered Desktop bundle identity, retained by its generated launcher across respawns. */
  expectedBundleRoot?: string;
  macBundleTrustReason?: (bundleRoot: string, binary: string) => string | null;
}

export interface CodexExecBoundaryAsyncDeps extends Omit<CodexExecBoundaryDeps, "macBundleTrustReason"> {
  signal?: AbortSignal;
  /**
   * Async twin of the sync trust check. It MUST honor the forwarded signal:
   * once aborted it settles promptly, and any children it owns (dscl/codesign)
   * are killed and reaped before it settles - the boundary awaits it, so a
   * check that ignores the signal stalls the caller; one that abandons its
   * children leaks processes.
   */
  macBundleTrustReasonAsync?: (bundleRoot: string, binary: string, signal?: AbortSignal) => Promise<string | null>;
}

/** Classification of this exact validated invocation, not a reusable trust grant. */
interface ValidatedCodexExecTarget {
  readonly invocation: SpawnInvocation;
  readonly macAppBundle: boolean;
}

export function validatedCodexExecTarget(
  command: string,
  args: readonly string[],
  deps: CodexExecBoundaryDeps = {},
): ValidatedCodexExecTarget {
  if (deps.expectedBundleRoot !== undefined) throw new CodexExecTrustError("macOS app bundles are not supported");
  return { invocation: codexExecInvocation(command, args, deps.platform ?? process.platform, { env: deps.env, exists: deps.exists }), macAppBundle: false };
}

export async function validatedCodexExecTargetAsync(
  command: string,
  args: readonly string[],
  deps: CodexExecBoundaryAsyncDeps = {},
): Promise<ValidatedCodexExecTarget> {
  deps.signal?.throwIfAborted();
  if (deps.expectedBundleRoot !== undefined) throw new CodexExecTrustError("macOS app bundles are not supported");
  return { invocation: codexExecInvocation(command, args, deps.platform ?? process.platform, { env: deps.env, exists: deps.exists }), macAppBundle: false };
}

export function validatedCodexExecInvocation(
  command: string, args: readonly string[], deps: CodexExecBoundaryDeps = {},
): SpawnInvocation {
  return validatedCodexExecTarget(command, args, deps).invocation;
}

export async function validatedCodexExecInvocationAsync(
  command: string, args: readonly string[], deps: CodexExecBoundaryAsyncDeps = {},
): Promise<SpawnInvocation> {
  const target = await validatedCodexExecTargetAsync(command, args, deps);
  deps.signal?.throwIfAborted();
  return target.invocation;
}

/** Assert a single already-located executable; returns the canonical target to exec. */
export function assertCodexExecTargetTrusted(command: string, deps: CodexExecBoundaryDeps = {}): string {
  return validatedCodexExecInvocation(command, [], deps).file;
}

export async function assertCodexExecTargetTrustedAsync(
  command: string,
  deps: CodexExecBoundaryAsyncDeps = {},
): Promise<string> {
  return (await validatedCodexExecInvocationAsync(command, [], deps)).file;
}
