import type { CapturedAdmissionPolicy } from "../types/admission-policy";
import { internalCallCapability, isInternalCallCapability } from "../lib/local-internal-call-capability";
import { immutableAdmissionPolicy } from "./capture-admission-policy";

export const SIDECAR_ADMISSION_POLICY_HEADER = "x-opencodex-sidecar-admission-policy";
export const SIDECAR_INTERNAL_CAPABILITY_HEADER = "x-opencodex-sidecar-internal";
const policies = new Map<string, { capability: string; policy: CapturedAdmissionPolicy }>();

/** A helper request owns its registry entry until completion, failure or cancellation. */
export function registerSidecarAdmissionPolicy(policy: CapturedAdmissionPolicy, signal?: AbortSignal) {
  const token = Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url");
  const release = () => {
    policies.delete(token);
    signal?.removeEventListener("abort", release);
  };
  if (!signal?.aborted) {
    policies.set(token, { capability: internalCallCapability(), policy: immutableAdmissionPolicy(policy) });
    signal?.addEventListener("abort", release, { once: true });
  }
  return { token, release };
}

export function readSidecarAdmissionPolicy(token: string | null, capability: string | null): CapturedAdmissionPolicy | undefined {
  if (!token || !isInternalCallCapability(capability)) return undefined;
  const entry = policies.get(token);
  return entry?.capability === capability ? entry.policy : undefined;
}

