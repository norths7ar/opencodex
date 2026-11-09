import { readFileSync } from "node:fs";
import {
  inspectPickerTrustWindows,
  trustPickerCaWindows,
  untrustPickerCaWindows,
} from "./picker-trust-windows";

/** Trust is scoped to the current root fingerprint and a verified persisted leaf. */
export type PickerTrustState = "trusted" | "untrusted" | "unsupported" | "unknown";
export interface SecurityResult { code: number | null; stdout: string; stderr: string }
export type SecurityRunner = (args: readonly string[]) => Promise<SecurityResult>;

export async function inspectPickerTrust(
  leafPath: string,
  caSha1: string,
  run?: SecurityRunner,
  platform: NodeJS.Platform = process.platform,
): Promise<PickerTrustState> {
  if (platform === "win32") {
    return inspectPickerTrustWindows(leafPath, caSha1, run);
  }
  return "unsupported";
}

export async function trustPickerCa(
  caPath: string,
  run?: SecurityRunner,
  platform: NodeJS.Platform = process.platform,
  cert?: { pem?: string },
): Promise<{ ok: boolean; reason?: "unsupported" | "declined_or_failed" }> {
  if (platform === "win32") {
    return trustPickerCaWindows(caPath, run, cert);
  }
  return { ok: false, reason: "unsupported" };
}

export async function untrustPickerCa(
  caPath: string,
  fingerprintSha1: string,
  run?: SecurityRunner,
  platform: NodeJS.Platform = process.platform,
): Promise<{ ok: boolean }> {
  if (platform === "win32") {
    try {
      return await untrustPickerCaWindows(fingerprintSha1, run,
        { pem: readFileSync(caPath, "utf8") });
    } catch { return { ok: false }; }
  }
  return { ok: false };
}
