import { redactSecretArgs } from "./secret-args";
import { codexExecInvocation } from "../codex/exec-invocation";
import { validatedCodexExecInvocationAsync, type CodexExecBoundaryAsyncDeps } from "../codex/exec-boundary";
import { resolveAndPersistCodexRuntime, type ResolveCodexRuntimeDeps } from "../codex/runtime";
import { withCodexCliSelectionParentWriteAsync } from "../codex/cli-selection";
import type { ResolveDeps, SpawnInvocation } from "../lib/win-exec";
import {
  apiError,
  apiJson,
  proxyUnreachable,
  resolveBaseUrl,
  type AccountDeps,
} from "./account-api";

const USAGE = `Usage:
  ocx account main reauth --device [--no-wait] [--json]
  ocx account main reauth status --flow <id> [--json]
  ocx account main reauth cancel --flow <id> [--json]

Reauthenticate the same native Codex App/CLI identity.`;

export type NativeMainCodexLoginInvocationDeps =
  & ResolveDeps
  & Pick<ResolveCodexRuntimeDeps, "existsSync" | "execFileSync" | "configDir" | "readFileSync">;

export type NativeMainCodexLoginValidatedDeps =
  & NativeMainCodexLoginInvocationDeps
  & CodexExecBoundaryAsyncDeps;

/**
 * Resolve+persist the runtime selection, then let the exec boundary produce the
 * invocation that is spawned. Validating the raw command matters: re-wrapping
 * the already-prepared invocation would launch cmd.exe as if it were Codex and
 * drop windowsVerbatimArguments.
 */
export async function nativeMainCodexLoginValidatedInvocation(
  platform: NodeJS.Platform = process.platform,
  deps: NativeMainCodexLoginValidatedDeps = {},
): Promise<SpawnInvocation> {
  const env = deps.env ?? process.env;
  const command = (await withCodexCliSelectionParentWriteAsync(() => resolveAndPersistCodexRuntime({
    env,
    platform,
    existsSync: deps.existsSync,
    execFileSync: deps.execFileSync,
    configDir: deps.configDir,
    readFileSync: deps.readFileSync,
    discoverAlternatives: false,
  }), { configDir: deps.configDir, signal: deps.signal })).runtime.command || "codex";
  return validatedCodexExecInvocationAsync(command, ["login"], {
    env,
    platform,
    exists: deps.exists,
    executable: deps.executable,
    realpath: deps.realpath,
    macBundleTrustReasonAsync: deps.macBundleTrustReasonAsync,
    signal: deps.signal,
  });
}

function flag(args: string[], name: string): boolean {
  const index = args.indexOf(name);
  if (index < 0) return false;
  args.splice(index, 1);
  return true;
}

function reject(args: string[]): number {
  if (args.length > 0) console.error(`Unexpected argument(s): ${redactSecretArgs(args).join(", ")}`);
  console.error(USAGE);
  return 1;
}

export function nativeMainCodexLoginInvocation(
  platform: NodeJS.Platform = process.platform,
  deps: NativeMainCodexLoginInvocationDeps = {},
): SpawnInvocation {
  const command = resolveAndPersistCodexRuntime({
    env: deps.env ?? process.env,
    platform,
    existsSync: deps.existsSync,
    execFileSync: deps.execFileSync,
    configDir: deps.configDir,
    readFileSync: deps.readFileSync,
    discoverAlternatives: false,
  }).runtime.command || "codex";
  return codexExecInvocation(command, ["login"], platform, deps);
}

export async function cmdNativeMainAccount(args: string[], deps: AccountDeps): Promise<number> {
  if (args.shift() !== "reauth") return reject([]);
  const wantsJson = flag(args, "--json");
  const baseUrl = await resolveBaseUrl(deps);
  if (!baseUrl) return proxyUnreachable();

  const noWait = flag(args, "--no-wait");
  const device = flag(args, "--device");
  const flowIndex = args.indexOf("--flow");
  const flowId = flowIndex >= 0 ? args.splice(flowIndex, 2)[1] : undefined;
  const action = args.shift();
  if (args.length > 0) return reject(args);

  const printStatus = (status: Record<string, unknown>): void => {
    if (wantsJson) { console.log(JSON.stringify(status, null, 2)); return; }
    console.log("status: " + String(status.status ?? "unknown"));
    if (typeof status.verificationUrl === "string" && status.verificationUrl) {
      console.log("url: " + status.verificationUrl);
    }
    if (typeof status.deviceCode === "string" && status.deviceCode) {
      console.log("deviceCode: " + status.deviceCode);
    }
    if (typeof status.code === "string" && status.code) {
      console.log("failure: " + status.code);
    }
  };

  if (action === "status" || action === "cancel") {
    if (!flowId || device || noWait) return reject(args);
    const query = "/api/codex-auth/main/reauth-device?flowId=" + encodeURIComponent(flowId);
    const result = action === "status"
      ? await apiJson(deps, baseUrl, "GET", query)
      : await apiJson(deps, baseUrl, "DELETE", query);
    if (result.status === 0) return proxyUnreachable(result.transportError);
    if (result.status !== 200) return apiError(result.json, "failed to " + action + " the native main reauth", result.status);
    printStatus(result.json);
    return 0;
  }

  if (action !== undefined || !device) return reject(args);
  const started = await apiJson(deps, baseUrl, "POST", "/api/codex-auth/main/reauth-device");
  if (started.status === 0) return proxyUnreachable(started.transportError);
  if (started.status !== 200) return apiError(started.json, "failed to start the native main device reauth", started.status);
  const startFlowId = typeof started.json.flowId === "string" ? started.json.flowId : "";
  if (!startFlowId) {
    console.error("The proxy returned an invalid reauth flow.");
    return 1;
  }
  // The URL and human code arrive with the usercode response, a beat after start.
  let pending = started.json;
  const statusQuery = "/api/codex-auth/main/reauth-device?flowId=" + encodeURIComponent(startFlowId);
  for (let attempt = 0; attempt < 50 && !pending.deviceCode; attempt += 1) {
    await Bun.sleep(200);
    const polled = await apiJson(deps, baseUrl, "GET", statusQuery);
    if (polled.status === 200) pending = polled.json;
  }
  if (noWait) {
    printStatus({ flowId: startFlowId, ...pending });
    if (!wantsJson) console.log("follow up: ocx account main reauth status --flow " + startFlowId);
    return 0;
  }
  // Blocking wait bounded by the service flow expiry (15-minute grant + margin).
  const deadline = Date.now() + 16 * 60_000;
  for (;;) {
    if (pending.status !== "pending" && pending.status !== "committing") break;
    if (Date.now() >= deadline) {
      console.error("The reauth flow did not finish within the device grant window; check status with --flow " + startFlowId + ".");
      return 1;
    }
    await Bun.sleep(2_000);
    const polled = await apiJson(deps, baseUrl, "GET", statusQuery);
    if (polled.status === 0) return proxyUnreachable(polled.transportError);
    if (polled.status !== 200) return apiError(polled.json, "failed to poll the native main reauth", polled.status);
    pending = polled.json;
  }
  printStatus({ flowId: startFlowId, ...pending });
  return pending.status === "succeeded" ? 0 : 1;
}
