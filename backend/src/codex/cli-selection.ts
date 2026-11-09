import { resolve } from "node:path";
import { getConfigDir } from "../config/paths";

export class CodexCliSelectionMutationBusyError extends Error {
  constructor() {
    super("codex_cli_selection_busy");
    this.name = "CodexCliSelectionMutationBusyError";
  }
}

export interface CodexCliSelectionParentWrite {
  readonly entered: boolean;
  exit(): void;
}

type SynchronousCommit<T> = () => T & (T extends PromiseLike<unknown> ? never : unknown);
export interface CodexCliSelectionAsyncParentWrite extends CodexCliSelectionParentWrite {
  commit<T>(run: SynchronousCommit<T>): T;
}

let owner: symbol | null = null;
let commitHome: string | null = null;
const refused: CodexCliSelectionAsyncParentWrite = {
  entered: false,
  exit() {},
  commit() { throw new CodexCliSelectionMutationBusyError(); },
};

/** A held selection write excludes a concurrent pin, shim change, or client launch. */
export function beginCodexCliSelectionParentWrite(configDir = getConfigDir()): CodexCliSelectionParentWrite {
  if (commitHome !== null) return { entered: resolve(configDir) === commitHome, exit() {} };
  if (owner !== null) return refused;
  const token = owner = Symbol();
  return { entered: true, exit() { if (owner === token) owner = null; } };
}

interface AdmissionOptions {
  configDir?: string;
  signal?: AbortSignal;
}

/** Hold the write reservation across preparation; synchronous nested writers may join the commit. */
export async function beginCodexCliSelectionParentWriteAsync(options: AdmissionOptions = {}): Promise<CodexCliSelectionAsyncParentWrite> {
  if (options.signal?.aborted || owner !== null) return refused;
  const home = resolve(options.configDir ?? getConfigDir());
  const token = owner = Symbol();
  return {
    entered: true,
    exit() { if (owner === token) owner = null; },
    commit<T>(run: SynchronousCommit<T>): T {
      if (owner !== token || options.signal?.aborted) throw new CodexCliSelectionMutationBusyError();
      const previous = commitHome;
      commitHome = home;
      try { return run(); } finally { commitHome = previous; }
    },
  };
}

export async function withCodexCliSelectionParentWriteAsync<T>(run: SynchronousCommit<T>, options: AdmissionOptions = {}): Promise<T> {
  const write = await beginCodexCliSelectionParentWriteAsync(options);
  try { return write.commit<T>(run); } finally { write.exit(); }
}
