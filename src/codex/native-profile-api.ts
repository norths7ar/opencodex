import type { OcxConfig } from "../types";
import { jsonResponse } from "../server/auth-cors";
import type { NativeProfileManager } from "./native-profile-manager";
import { nativeMainStartupGateSnapshot } from "./native-profile-startup";
import type { blockNativeMainRecovery, completeNativeMainRecovery } from "./native-profile-startup";
import type { probeNativeProfileRecoveryState } from "./native-profile-store";

export interface NativeProfileApiDeps {
  manager?: NativeProfileManager;
  drainTimeoutMs?: number;
  sleep?: (ms: number) => Promise<void>;
  probeRecoveryState?: typeof probeNativeProfileRecoveryState;
  blockRecovery?: typeof blockNativeMainRecovery;
  completeRecovery?: typeof completeNativeMainRecovery;
}

function admissionFields(): { admissionStatus: "ready" | "blocked"; admissionBlockReason?: string } {
  const gate = nativeMainStartupGateSnapshot();
  return gate.status === "blocked"
    ? { admissionStatus: gate.status, admissionBlockReason: gate.reason }
    : { admissionStatus: gate.status };
}

export async function handleNativeProfileAPI(
  req: Request,
  url: URL,
  config: OcxConfig,
  _deps: NativeProfileApiDeps = {},
): Promise<Response | null> {
  const namespace = "/api/native-main-profiles";
  if (url.pathname !== namespace && !url.pathname.startsWith(namespace + "/")) return null;
  // Admission is read from this process's startup gate only. It constructs no manager and runs
  // no doctor sweep, so the single-use local read capability `ocx doctor` holds can reach it
  // without reaching staging cleanup, profile ids or filesystem paths.
  if (url.pathname === "/api/native-main-profiles/admission" && req.method === "GET") {
    return jsonResponse(admissionFields(), 200, req, config);
  }
  return jsonResponse({ error: "Native login profiles are not supported" }, 410, req, config);
}
