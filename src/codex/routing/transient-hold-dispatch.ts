import { isCodexAccountGenerationLive } from "../account-store";
// From `../account-id`, which declares the constant and imports nothing, rather than from
// `../main-account`, which re-exports it and sits inside the routing/account-lifecycle import
// cycle. Neither reference here runs at module load, but a leaf import keeps this module out of
// that cycle entirely instead of relying on that staying true.
import { MAIN_CODEX_ACCOUNT_ID } from "../account-id";
import {
  invalidateTransientProbe,
  releaseTransientProbe,
  settleTransientProbe,
} from "../../routing/probe-lease";
import {
  CODEX_TRANSIENT_AFFINITY_HOLD_MS,
  type ThreadAffinityEntry,
  type TransientProbeGrant,
} from "./thread-affinity";
import type { CodexUpstreamOutcomeClass, CodexUpstreamOutcomeMeta } from "./cooldown-math";

/** Has a held binding waited longer than a transient failure can reasonably explain? */
export function isTransientHoldExpired(entry: ThreadAffinityEntry, now: number): boolean {
  return entry.transientHoldSince !== undefined
    && now - entry.transientHoldSince > CODEX_TRANSIENT_AFFINITY_HOLD_MS;
}

/** Does the credential a probe was granted against still exist at that generation? */
function transientProbeCredentialLive(accountId: string, generation: number): boolean {
  if (accountId === MAIN_CODEX_ACCOUNT_ID) return generation === 0;
  return isCodexAccountGenerationLive(accountId, generation);
}

/**
 * Conclude the half-open recovery probe this request was holding.
 *
 * Three answers, because three things can be true of a probe that just ended:
 *
 * - The credential moved under it. Its result describes an identity the binding no longer has,
 *   so the epoch is BURNED instead of settled -- invalidating makes every outstanding lease on
 *   this account stale at once, which is what stops a late answer from reviving a dead account.
 * - The answer says nothing about the account. A 3xx, a 400, or an unclassifiable status is the
 *   request's problem, not the account's, so the lease is handed back unspent and the next
 *   request may run a real trial instead of waiting out a recovery nobody observed.
 * - Otherwise it is evidence: success means recovered, everything else means still failing.
 *
 * A no-op when this request held no trial, so the outcome recorder calls it unconditionally.
 */
export function settleTransientProbeForOutcome(
  accountId: string,
  meta: Pick<CodexUpstreamOutcomeMeta, "transientProbe" | "now">,
  outcomeClass: CodexUpstreamOutcomeClass,
): void {
  const grant: TransientProbeGrant | undefined = meta.transientProbe;
  if (!grant) return;
  if (!transientProbeCredentialLive(accountId, grant.affinityGeneration)) {
    invalidateTransientProbe(accountId);
    return;
  }
  if (outcomeClass === "neutral" || outcomeClass === "caller" || outcomeClass === "unknown") {
    releaseTransientProbe(grant.lease);
    return;
  }
  settleTransientProbe(grant.lease, outcomeClass === "success" ? "recovered" : "failed", meta.now ?? Date.now());
}
