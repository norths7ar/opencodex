import { singleCodexAccountId } from "./account-id";
import { isCodexAccountGenerationLive, registerCodexRefreshGenerationHandoff } from "./account-store";
import { handOffThreadAffinityGeneration } from "./routing/thread-affinity";
import { codexAccountLogLabel } from "./account-label";
import { type CodexAccountUsabilityOptions } from "./account-usability";
import { markAccountNeedsReauth } from "./account-runtime-state";
import { isRetiredCodexSparkModel } from "./quota";
import { MAIN_CODEX_ACCOUNT_ID } from "./main-account";
import { isSelectableCodexPoolAccount } from "./account-id";
import type { OcxConfig } from "../types";
import { captureConfigGeneration, type GenerationContext } from "../lib/state-store-sweeper";
import { recordUpstreamHostFailure } from "./upstream-host-health";
import type { CodexThreadLineage } from "./lineage";
import {
  forgetCodexFailureWindow,
  forgetCodexFailureWindows,
  noteCodexFailureWindowOutcome,
  settleCodexSuccessStreak,
} from "./routing/failure-window";

import {
  classifyCodexUpstreamOutcome,
  computeCodexUsageScore,
  computeQuotaCooldown,
  quotaAvoidUntilFor,
  CODEX_FAILURE_WINDOW_MS,
  CODEX_TRANSIENT_SOFT_AVOID_ESCALATION_MS,
  type CodexUpstreamOutcome,
  type CodexUpstreamOutcomeMeta,
} from "./routing/cooldown-math";
import {
  carriesQuotaRefusal,
  codexPoolKeyForScope,
  codexQuotaScopeForModel,
  deleteAccountHealth,
  deleteAllScopedHealth,
  deleteScopedHealth,
  dropSpentCredentialFailure,
  getAccountHealth,
  getCodexAccountCooldownUntil,
  getCodexAccountSoftAvoidUntil,
  getCodexQuotaHealthSnapshot,
  isCodexAccountSoftAvoided,
  isHealthAccountAdmissible,
  isHealthGenerationReconciled,
  preservedCooldownFields,
  pruneHealthAccountsForContext,
  commitHealthReconcile,
  clearUpstreamHealthState,
  resetHealthReconcileState,
  deleteAllHealthForAccount,
  scopedHealthFor,
  setAccountHealth,
  setScopedHealth,
  type CodexQuotaScope,
} from "./routing/health-store";
import { ownsProbeLease, probeMayClearCooldown, withProbeLeaseReleased } from "./routing/probe-lease";
// `./routing/probe-lease` above is the QUOTA-COOLDOWN lease; the module below owns the
// unrelated TRANSIENT-HOLD trial and the pool-wide recovery bound above it (#4701).
import { isTransientHoldExpired, settleTransientProbeForOutcome } from "./routing/transient-hold-dispatch";
import {
  clearThreadAccountMapForAccount,
  deleteThreadAffinitiesForAccount,
  getThreadAffinityScopes,
  CODEX_THREAD_AFFINITY_REEVAL_INTERVAL_MS,
  CODEX_TRANSIENT_AFFINITY_HOLD_MS,
  type CodexAffinityReason,
  type CodexThreadResolution,
} from "./routing/thread-affinity";
import { isCodexAccountSelectable } from "./routing/selection";
import {
  clearAllManualPreferences,
  consumeManualPreference,
  forgetManualPreference,
  forgetRoutingPreferencesOutside,
  getEffectiveActiveCodexAccountId,
} from "./routing/active-account";

export {
  CODEX_QUOTA_PROBE_INTERVAL_MS,
  CODEX_FAILURE_WINDOW_MS,
  TERMINAL_SHORT_WINDOW_FRESHNESS_MS,
  CODEX_TRANSIENT_SOFT_AVOID_MS,
  classifyCodexUpstreamOutcome,
  computeCodexUsageScore,
  computeQuotaCooldown,
  computeQuotaCooldownUntil,
  parseRetryAfterMs,
  parseResetCooldownMs,
} from "./routing/cooldown-math";
export type {
  CodexUpstreamOutcome,
  CodexUpstreamOutcomeClass,
  CodexCooldownSource,
  CodexUpstreamOutcomeMeta,
} from "./routing/cooldown-math";
export {
  codexQuotaScopeForModel,
  listLiveCodexAccountIds,
  getCodexUpstreamHealth,
  getCodexAccountCooldownUntil,
  getCodexAccountHealthSnapshot,
  getCodexQuotaHealthSnapshot,
  isCodexAccountInCooldown,
  clearCodexAccountCooldown,
  getCodexAccountSoftAvoidUntil,
  isCodexAccountSoftAvoided,
} from "./routing/health-store";
export type { CodexQuotaScope } from "./routing/health-store";
export {
  tryAcquireCodexQuotaProbeLease,
  canAcquireCodexQuotaProbeLease,
  claimDueCodexQuotaRecoveryProbes,
  claimManualResetCooldowns,
  settleManualResetCooldown,
  settleCodexQuotaRecoveryProbe,
  tryAcquireCodexQuotaScopeProbeLease,
  canAcquireCodexQuotaScopeProbeLease,
  releaseCodexQuotaProbeLease,
  releaseCodexQuotaScopeProbeLease,
} from "./routing/probe-lease";
export type {
  CodexQuotaRecoveryProbeClaim,
  CodexQuotaRecoveryProbeProof,
  ManualResetCooldownClaim,
  ManualResetRefreshLineage,
} from "./routing/probe-lease";
export {
  CODEX_THREAD_AFFINITY_IDLE_TTL_MS,
  CODEX_THREAD_AFFINITY_MAX_ENTRIES,
  CODEX_THREAD_AFFINITY_REEVAL_INTERVAL_MS,
  CODEX_TRANSIENT_AFFINITY_HOLD_MS,
  clearConversationStateIssuerMap,
  clearThreadAccountMap,
  clearThreadAccountMapForAccount,
  debugCodexAffinityGenerations,
  handOffThreadAffinityGeneration,
  peekConversationStateIssuer,
  rememberConversationStateIssuer,
} from "./routing/thread-affinity";
export type {
  CodexThreadResolution,
  CodexAffinityMove,
  CodexAffinityReason,
  CodexAffinityDecision,
  TransientProbeGrant,
} from "./routing/thread-affinity";
export {
  isCodexAccountPlanExcluded,
  getPoolAccountPlan,
} from "./routing/selection";
export {
  resetCodexRoutingForManualSelection, resetCodexRoutingForClearedSelection,
  getEffectiveActiveCodexAccountId,
  isEffectiveCodexAccountPinned,
} from "./routing/active-account";
export { codexFailureWindowOutcomeMeta } from "./routing/failure-window";

// A shared refresh can outlive the request that opened it. Register the affinity
// handoff with the flight so a detached G -> G+1 commit cannot strand bindings at G.
registerCodexRefreshGenerationHandoff(handOffThreadAffinityGeneration);

export function clearCodexUpstreamHealth(): void {
  // Reset explicit selection preferences alongside account health.
  clearAllManualPreferences();
  clearUpstreamHealthState();
  forgetCodexFailureWindows();
  // The reconcile watermark is part of this state, not something that outlives it. Keeping
  // it across a full reset is incoherent: there is no health left to protect, yet
  // recordCodexUpstreamOutcome would still drop a writer whose generation predates the
  // watermark for any account missing from the equally stale live set. Left behind, it also
  // leaks between test files, which is how it was found.
  resetHealthReconcileState();
}

export function clearCodexUpstreamHealthForAccount(accountId: string): void {
  deleteAllHealthForAccount(accountId);
  forgetCodexFailureWindow(accountId);
  // An explicit selection preference cannot outlive the account it names.
  forgetManualPreference(accountId);
}

export function reconcileCodexRoutingHealth(context: GenerationContext): number {
  if (isHealthGenerationReconciled(context.generation)) return 0;
  const removed = pruneHealthAccountsForContext(context.codexAccountIds);
  // Sweep preferences the same way, for the account set this generation actually has. The
  // delete path above is the direct route; this is the one that catches an account removed
  // by an edit the runtime never saw. Deliberately not counted in `removed`, which reports
  // health rows.
  forgetRoutingPreferencesOutside(context.codexAccountIds);
  commitHealthReconcile(context.generation, context.codexAccountIds);
  return removed;
}

/** Whether every matching thread affinity has outlived its transient hold. */
function isTransientHoldSpentForAccount(threadId: string, accountId: string, now: number): boolean {
  const affinities = getThreadAffinityScopes(threadId);
  if (!affinities) return false;
  let matched = false;
  for (const entry of affinities.values()) {
    if (entry.accountId !== accountId) continue;
    matched = true;
    if (!isTransientHoldExpired(entry, now)) return false;
  }
  return matched;
}

/** An excluded account remains selected for explicit resume or reauthentication. */
export function reconcileCodexActiveAfterExclusion(
  config: OcxConfig,
  excludedAccountId: string,
  now = Date.now(),
): string | null {
  // Keep the signed-in identity visible for resume/reauthentication. A pause never
  // selects another stored credential or silently falls back to native main.
  forgetManualPreference(excludedAccountId);
  return null;
}

/** Preview the same single channel account used by final dispatch, without mutating it. */
export function previewCodexAccountForRequest(
  threadId: string | null,
  config: OcxConfig,
  now = Date.now(),
  quotaScope?: CodexQuotaScope,
  selectionOptions?: CodexAccountUsabilityOptions,
  modelId?: string,
  lineage?: CodexThreadLineage,
): string | null {
  if (config.codexSignedOut === true) return null;
  const selected = config.providers.openai?.codexAccountMode === "direct"
    ? MAIN_CODEX_ACCOUNT_ID : singleCodexAccountId(config);
  return isCodexAccountSelectable(config, selected, now, quotaScope, selectionOptions) ? selected : null;
}


export function recordCodexUpstreamOutcome(
  config: OcxConfig,
  accountId: string | null,
  outcome: CodexUpstreamOutcome,
  meta: CodexUpstreamOutcomeMeta = {},
): void {
  // Host-level evidence is account-independent (#914): a pre-connection
  // reachability failure is recorded in the (provider, host) ledger even when
  // there is no account to attribute, or the account's writer generation is
  // stale — the early returns below must not gate it.
  if (outcome === "connect_neutral" && meta.hostKey) {
    recordUpstreamHostFailure(meta.hostKey, { code: meta.lastFailureCode, now: meta.now ?? Date.now() });
  }
  if (!accountId) return;
  // Conclude the half-open recovery trial BEFORE the admissibility gate below (#4701): an
  // outcome that gate drops still ended this request, and a lease nobody hands back leaves the
  // next trial waiting out its deadline. The settle carries its own fences, so this is safe here.
  settleTransientProbeForOutcome(accountId, meta, classifyCodexUpstreamOutcome(outcome, meta.denial));
  const writerGeneration = meta.writerGeneration ?? captureConfigGeneration();
  if (!isHealthAccountAdmissible(accountId, writerGeneration)) return;
  const now = meta.now ?? Date.now();
  const outcomeClass = classifyCodexUpstreamOutcome(outcome, meta.denial);
  // Reject retired quota evidence before stale-credential cleanup or any shared mutation.
  if (outcomeClass === "quota" && isRetiredCodexSparkModel(meta.modelId)
      && computeQuotaCooldown(meta).source === "reset-derived") return;
  const quotaScope = codexQuotaScopeForModel(meta.modelId);
  /*
   * Spend a stale credential failure BEFORE any branch reads health (#2892 gap 4 review).
   *
   * Reader-side spending alone is not enough: the transient and workspace branches derive their new
   * entry from the current one, so a spent G1 401 would donate its `consecutiveFailures` to G2's
   * first genuine 503 and drop the tag while doing it. The account then reaches the failover
   * threshold one failure early, and no later read can tell. Clearing it here means every branch
   * starts from evidence that still describes a live credential.
   */
  dropSpentCredentialFailure(accountId);
  if (outcomeClass === "success") {
    noteCodexFailureWindowOutcome(config, accountId, false, now, meta);
    // The operator's one-shot is spent by a dispatch that actually worked, and only by that.
    // A failed lookup leaves it unspent so the intent survives the failure.
    consumeManualPreference(accountId, codexPoolKeyForScope(quotaScope));
    const scopedProbe = meta.probeQuotaScope
      ? scopedHealthFor(accountId, meta.probeQuotaScope)
      : undefined;
    if (scopedProbe && meta.probeQuotaScope) {
      if (scopedProbe.cooldownUntil && probeMayClearCooldown(scopedProbe, meta)) {
        deleteScopedHealth(accountId, meta.probeQuotaScope);
      } else if (ownsProbeLease(scopedProbe, meta)) {
        setScopedHealth(accountId, meta.probeQuotaScope, withProbeLeaseReleased(scopedProbe, now));
      }
    }
    // A served request is what ends the refusal marker the quota branch left on this lane.
    // The probe contract above owns the scoped COOLDOWN; this owns only the field
    // {@link hasUnrecoveredCodexQuotaRefusal} reads, which would otherwise keep threads away
    // from an account that is demonstrably serving them again. The account-wide marker needs
    // no equivalent: every recovery write below runs it through preservedCooldownFields.
    const refusedScope = quotaScope ? scopedHealthFor(accountId, quotaScope) : undefined;
    if (quotaScope && refusedScope && carriesQuotaRefusal(refusedScope)) {
      const {
        lastFailureStatus: _refusal, lastFailureAt: _refusedAt, quotaAvoidUntil: _avoid, ...retained
      } = refusedScope;
      // A live cooldown and its probe bookkeeping survive; an entry that held nothing else goes.
      if (Object.keys(retained).length > 1) setScopedHealth(accountId, quotaScope, retained);
      else deleteScopedHealth(accountId, quotaScope);
    }
    const current = getAccountHealth(accountId);
    const cooldownUntil = getCodexAccountCooldownUntil(accountId, now);
    // A leased probe that is still on its own cooldown generation proves the
    // account recovered: clear the hard cooldown outright (#433).
    if (cooldownUntil && probeMayClearCooldown(current, meta)) {
      deleteAccountHealth(accountId);
      return;
    }
    // Owning probe on a stale generation: the lease is done, but a newer 429
    // replaced the cooldown in the meantime, so only give the lease back.
    // Non-owners keep every hard-cooldown field, including someone else's live lease.
    const base = ownsProbeLease(current, meta) ? withProbeLeaseReleased(current!, now) : current;
    const preserved = preservedCooldownFields(base);
    settleCodexSuccessStreak(config, accountId, current, base, preserved, cooldownUntil);
    return;
  }
  if (outcomeClass === "caller") {
    // A 4xx does not change account health, but it does conclude an in-flight
    // probe — otherwise the lease would never be handed back.
    const current = getAccountHealth(accountId);
    const scopedProbe = meta.probeQuotaScope
      ? scopedHealthFor(accountId, meta.probeQuotaScope)
      : undefined;
    if (scopedProbe && meta.probeQuotaScope && ownsProbeLease(scopedProbe, meta)) {
      setScopedHealth(accountId, meta.probeQuotaScope, withProbeLeaseReleased(scopedProbe, now));
    }
    if (ownsProbeLease(current, meta)) {
      setAccountHealth(accountId, withProbeLeaseReleased(current!, now));
    }
    return;
  }

  if (outcomeClass === "neutral") {
    // A proven pre-connection reachability failure (DNS / TCP refusal) or a
    // relayed 3xx is host-level, not account evidence: rotation cannot repair
    // it and must not happen (#914). Conclude any owned probe lease, record the
    // failure under the (provider, host) ledger when one is named, and leave
    // account health, thread affinity, and the active account untouched.
    const current = getAccountHealth(accountId);
    const scopedProbe = meta.probeQuotaScope
      ? scopedHealthFor(accountId, meta.probeQuotaScope)
      : undefined;
    if (scopedProbe && meta.probeQuotaScope && ownsProbeLease(scopedProbe, meta)) {
      setScopedHealth(accountId, meta.probeQuotaScope, withProbeLeaseReleased(scopedProbe, now));
    }
    if (ownsProbeLease(current, meta)) {
      setAccountHealth(accountId, withProbeLeaseReleased(current!, now));
    }
    return;
  }

  const lastFailureStatus = typeof outcome === "number" ? outcome : 0;
  if (outcomeClass === "workspace") {
    // The credential is valid; this account just cannot reach this workspace (#1789).
    // Record the failure so routing stops preferring it, but do not mark it for
    // reauthentication and do not sweep its thread affinities: telling the user to
    // re-login is wrong advice that cannot fix a workspace grant.
    setAccountHealth(accountId, {
      consecutiveFailures: (getAccountHealth(accountId)?.consecutiveFailures ?? 0) + 1,
      lastFailureStatus,
      lastFailureAt: now,
    });
    return;
  }
  if (outcomeClass === "credential") {
    // 401/403 quarantines the account for reauth. That supersedes quota state
    // entirely: a cooldown (and any probe lease) on an unusable account is moot.
    // Unless the rejected credential is already gone: a stale 401 racing a
    // replacement would otherwise take the fresh credential out of rotation and
    // sweep affinities that belong to it (#2887).
    if (
      meta.credentialGeneration !== undefined
      && !isCodexAccountGenerationLive(accountId, meta.credentialGeneration)
    ) {
      return;
    }
    /*
     * The pre-check above closes the same-process race, but not a cross-process one (#2892 gap 4).
     * `isCodexAccountGenerationLive` is an unlocked read while credential writers coordinate under
     * the mutation lock, and OS preemption needs no `await` — so another process can replace the
     * credential after this check, or at any point after this whole function returns. No re-read here
     * can close that: a replacement is always free to land one instruction later.
     *
     * Taking the credential lock is not an option either: it runs with `busy_timeout=0`, so acquiring
     * it per outcome would turn ordinary contention into thrown request-path errors.
     *
     * So the evidence is TAGGED with the credential it describes and judged when it is READ. The
     * health entry carries `credentialFailureGeneration` and the reauth map carries the same
     * generation; `dropSpentCredentialFailure` and `isAccountNeedsReauth` discard an entry whose
     * credential is gone. A later transient or quota write replaces the entry along with its tag, and
     * `preservedCooldownFields` drops the tag explicitly, so this provenance can never be spent
     * against a failure it did not describe.
     *
     * Affinity sweeping needs no tag: an affinity entry already carries a credential generation and
     * self-invalidates on the next check, and re-adding swept entries would be a worse bug.
     */
    setAccountHealth(accountId, {
      consecutiveFailures: 1,
      lastFailureStatus,
      lastFailureAt: now,
      // Provenance rides on the entry: only this failure can be spent when its credential dies.
      ...(meta.credentialGeneration !== undefined
        ? { credentialFailureGeneration: meta.credentialGeneration }
        : {}),
    });
    deleteAllScopedHealth(accountId);
    // The reauth flag carries the same provenance, so a replacement landing after this call cannot
    // inherit a quarantine that was never about it.
    markAccountNeedsReauth(accountId, writerGeneration, meta.credentialGeneration);
    clearThreadAccountMapForAccount(accountId, "quota_refusal");
    return;
  }

  if (outcomeClass === "quota") {
    // Outcome recording never changes the channel's signed-in account.
    const { until, source } = computeQuotaCooldown(meta);
    // A reset timestamp is an advisory quota-window announcement. When the
    // selected native model belongs to a confirmed independent group, preserve
    // it there so a different group (Reserve versus the shared native quota) can
    // still reach upstream. Explicit Retry-After/default 429s remain account-wide.
    if (source === "reset-derived" && quotaScope) {
      const prior = scopedHealthFor(accountId, quotaScope);
      const cooldownGeneration = (prior?.cooldownGeneration ?? 0) + 1;
      const ownsLease = meta.probeQuotaScope === quotaScope && ownsProbeLease(prior, meta);
      setScopedHealth(accountId, quotaScope, {
        consecutiveFailures: 0,
        lastFailureStatus,
        lastFailureAt: now,
        cooldownUntil: until,
        quotaAvoidUntil: quotaAvoidUntilFor(meta, now, until),
        cooldownSince: now,
        cooldownSource: source,
        cooldownGeneration,
        ...(ownsLease
          ? { lastProbeAt: now }
          : {
            ...(prior?.probeLeaseId !== undefined ? { probeLeaseId: prior.probeLeaseId } : {}),
            ...(prior?.probeLeaseGeneration !== undefined ? { probeLeaseGeneration: prior.probeLeaseGeneration } : {}),
            ...(prior?.lastProbeAt !== undefined ? { lastProbeAt: prior.lastProbeAt } : {}),
        }),
      });
      if (quotaScope === "shared" && !meta.fixedAccount) clearThreadAccountMapForAccount(accountId, "quota_refusal");
      return;
    }

    // A scoped probe that received an account-wide throttle is no longer live.
    const scopedProbe = meta.probeQuotaScope
      ? scopedHealthFor(accountId, meta.probeQuotaScope)
      : undefined;
    if (scopedProbe && meta.probeQuotaScope && ownsProbeLease(scopedProbe, meta)) {
      setScopedHealth(accountId, meta.probeQuotaScope, withProbeLeaseReleased(scopedProbe, now));
    }
    const prior = getAccountHealth(accountId);
    // Every cooldown write bumps the generation so a probe issued against the
    // previous cooldown can no longer clear this one (#433).
    const cooldownGeneration = (prior?.cooldownGeneration ?? 0) + 1;
    // A failed probe concludes its lease; an unrelated 429 leaves the live probe alone.
    const ownsLease = ownsProbeLease(prior, meta);
    setAccountHealth(accountId, {
      consecutiveFailures: 0,
      lastFailureStatus,
      lastFailureAt: now,
      cooldownUntil: until,
      quotaAvoidUntil: quotaAvoidUntilFor(meta, now, until),
      cooldownSince: now,
      cooldownSource: source,
      cooldownGeneration,
      ...(ownsLease
        ? { lastProbeAt: now }
        : {
          ...(prior?.probeLeaseId !== undefined ? { probeLeaseId: prior.probeLeaseId } : {}),
          ...(prior?.probeLeaseGeneration !== undefined ? { probeLeaseGeneration: prior.probeLeaseGeneration } : {}),
          ...(prior?.lastProbeAt !== undefined ? { lastProbeAt: prior.lastProbeAt } : {}),
        }),
    });
    if (!meta.fixedAccount) clearThreadAccountMapForAccount(accountId, "quota_refusal");
    return;
  }

  // transient (connect_error / timeout / 5xx)
  const current = getAccountHealth(accountId);
  const scopedProbe = meta.probeQuotaScope
    ? scopedHealthFor(accountId, meta.probeQuotaScope)
    : undefined;
  if (scopedProbe && meta.probeQuotaScope && ownsProbeLease(scopedProbe, meta)) {
    setScopedHealth(accountId, meta.probeQuotaScope, withProbeLeaseReleased(scopedProbe, now));
  }
  // A transient failure concludes an owning probe; an unrelated 5xx must not
  // consume someone else's live lease or drop hard-cooldown bookkeeping (#433).
  const transientBase = ownsProbeLease(current, meta) ? withProbeLeaseReleased(current!, now) : current;
  noteCodexFailureWindowOutcome(config, accountId, true, now, meta);
  const stale = current?.lastFailureAt ? now - current.lastFailureAt > CODEX_FAILURE_WINDOW_MS : false;
  const hardCooldownUntil = getCodexAccountCooldownUntil(accountId, now) ?? undefined;
  // A threshold of zero disables the transient soft-avoid period.
  const failoverThreshold = config.upstreamFailoverThreshold ?? 3;
  const consecutiveFailures = stale ? 1 : (current?.consecutiveFailures ?? 0) + 1;
  const failoverReady = failoverThreshold > 0 && consecutiveFailures >= failoverThreshold;
  const escalationMs = CODEX_TRANSIENT_SOFT_AVOID_ESCALATION_MS[
    Math.min(Math.max(consecutiveFailures - failoverThreshold, 0), CODEX_TRANSIENT_SOFT_AVOID_ESCALATION_MS.length - 1)
  ]!;
  const softAvoidUntil = failoverReady
    ? Math.max(
      getCodexAccountSoftAvoidUntil(accountId, now) ?? 0,
      now + escalationMs,
    )
    : undefined;
  setAccountHealth(accountId, {
    ...preservedCooldownFields(transientBase),
    consecutiveFailures,
    lastFailureStatus,
    lastFailureAt: now,
    ...(hardCooldownUntil ? { cooldownUntil: hardCooldownUntil } : {}),
    ...(softAvoidUntil !== undefined ? { softAvoidUntil } : {}),
  });
  // Retire an expired hold without removing a binding owned by a different account.
  if (
    !meta.fixedAccount
    && failoverReady
    && meta.threadId
    && isTransientHoldSpentForAccount(meta.threadId, accountId, now)
  ) {
    deleteThreadAffinitiesForAccount(meta.threadId, accountId);
  }
}

export function formatCodexProviderForLog(providerName: string, accountId: string | null, config: OcxConfig): string {
  if (!accountId) return providerName;
  // The main Codex login participates in rotation as "main-pool" (MAIN_CODEX_ACCOUNT_ID) but is the
  // same physical account as the "main" passthrough (null accountId). Log both under the base provider
  // name so usage/tokens aggregate into a single row instead of splitting into `chatgpt` + `chatgpt-main`.
  if (accountId === MAIN_CODEX_ACCOUNT_ID) return providerName;
  const account = (config.codexAccounts ?? [])
    .find(candidate => isSelectableCodexPoolAccount(candidate) && candidate.id === accountId);
  return account ? `${providerName}-${codexAccountLogLabel(account)}` : providerName;
}
