import { realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve, win32 } from "node:path";
import { expandUserPath } from "../config/paths";
import { redactUserPath } from "../lib/redact";

export type CodexHomeDeps = {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform | string;
  homedir?: () => string;
  statSync?: typeof statSync;
  realpathSync?: (path: string) => string;
};

function canonicalExistingCodexHome(path: string, deps: CodexHomeDeps): string {
  const stat = deps.statSync ?? statSync;
  try {
    if (!stat(path).isDirectory()) return path;
    // Use the portable resolver here rather than realpathSync.native. The
    // Windows Bun standalone runtime can reject a valid junction through the
    // native resolver, while the effective home still needs to be physical
    // before callers open auth.json or native-profile state.
    return (deps.realpathSync ?? realpathSync)(path);
  } catch {
    // Preserve the existing lexical-path behavior for missing or unreadable
    // homes; callers that require the directory still fail at their boundary.
    return path;
  }
}

export function defaultCodexHome(deps: CodexHomeDeps = {}): string {
  const home = (deps.homedir ?? homedir)();
  const defaultHome = join(home, ".codex");
  return canonicalExistingCodexHome(defaultHome, deps);
}

export function resolveCodexHomeDir(deps: CodexHomeDeps = {}): string {
  const raw = (deps.env ?? process.env).CODEX_HOME?.trim();
  if (raw) return resolve(expandUserPath(raw));
  return defaultCodexHome(deps);
}

export type OrcaCodexHomeDiagnostic = {
  applicable: boolean;
  mismatch: boolean;
  effectiveCodexHome: string;
  appCodexHome: string;
  orcaCodexHome: string | null;
  warning: string | null;
  action: string | null;
};

type OrcaCodexHomeDeps = CodexHomeDeps & {
  effectiveCodexHome?: string;
  appCodexHome?: string;
};

function normalizedWindowsPath(path: string): string {
  return path.trim().replaceAll("/", "\\").replace(/\\+$/, "").toLowerCase();
}

/**
 * High-confidence Orca/ChatGPT dual-home diagnosis. Explicit CODEX_HOME remains
 * authoritative; this only explains when an Orca-owned shell targets a home the
 * Windows ChatGPT/Codex app does not read.
 */
export function collectOrcaCodexHomeDiagnostic(deps: OrcaCodexHomeDeps = {}): OrcaCodexHomeDiagnostic {
  const platform = deps.platform ?? process.platform;
  const env = deps.env ?? process.env;
  const effectiveCodexHome = deps.effectiveCodexHome ?? resolveCodexHomeDir(deps);
  const appCodexHome = deps.appCodexHome
    ?? (platform === "win32" ? win32.join((deps.homedir ?? homedir)(), ".codex") : join((deps.homedir ?? homedir)(), ".codex"));
  const explicitHome = env.CODEX_HOME?.trim() ?? "";
  const orcaCodexHome = env.ORCA_CODEX_HOME?.trim() || null;
  const normalizedEffective = normalizedWindowsPath(effectiveCodexHome);
  const normalizedOrca = orcaCodexHome ? normalizedWindowsPath(orcaCodexHome) : "";
  const normalizedApp = normalizedWindowsPath(appCodexHome);
  const applicable = platform === "win32"
    && !!explicitHome
    && !!orcaCodexHome
    && normalizedEffective === normalizedOrca
    && /(?:^|\\)orca\\codex-runtime-home\\home$/i.test(normalizedOrca);
  const mismatch = applicable && normalizedEffective !== normalizedApp;
  const displayEffective = redactUserPath(effectiveCodexHome);
  const displayApp = redactUserPath(appCodexHome);
  const displayOrca = orcaCodexHome ? redactUserPath(orcaCodexHome) : null;
  return {
    applicable,
    mismatch,
    effectiveCodexHome: displayEffective,
    appCodexHome: displayApp,
    orcaCodexHome: displayOrca,
    warning: mismatch
      ? `CODEX_HOME targets Orca's runtime home (${displayEffective}), while the Windows ChatGPT/Codex app uses ${displayApp}; OpenCodex injection will not reach that app.`
      : null,
    action: mismatch
      ? "If a service was installed from Orca, run 'ocx service uninstall' in that original Orca shell first. Then in Command Prompt run set \"ORCA_CODEX_HOME=\" and set \"CODEX_HOME=%USERPROFILE%\\.codex\"; or in PowerShell run Remove-Item Env:ORCA_CODEX_HOME -ErrorAction SilentlyContinue; $env:CODEX_HOME = Join-Path $env:USERPROFILE '.codex'. Rerun the command, then reinstall with 'ocx service install'."
      : null,
  };
}
