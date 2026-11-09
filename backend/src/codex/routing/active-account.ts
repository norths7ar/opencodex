import { singleCodexAccountId } from "../account-id";
import { pinnedCodexAccountId } from "../account-priority";
import {
  POOL_KEY_CODEX,
  clearPoolRotationState,
  seedPoolRotationAccount,
} from "../pool-rotation";
import type { OcxConfig } from "../../types";
import { clearThreadAccountMap } from "./thread-affinity";
import {
  NATIVE_MODEL_QUOTA_SCOPES,
  codexPoolKeyForScope,
  deleteAccountHealth,
  deleteScopedHealth,
  getAccountHealth,
  isIndependentCodexQuotaScope,
  listScopedHealthEntries,
  preservedCooldownFields,
  setAccountHealth,
  setScopedHealth,
  type CodexUpstreamHealth,
} from "./health-store";

/** Manual selection resets transient routing evidence without bypassing a real 429 cooldown. */
export function resetCodexRoutingForManualSelection(accountId: string): void {
  clearThreadAccountMap({ preserveConversationStateIssuers: true });
  // Record the explicit selection as an unspent preference for each quota scope.
  manualPreference.set(POOL_KEY_CODEX, accountId);
  // Seed the RR ring so the next unbound new session honors the manually selected account
  // under round-robin (affinity-cleared threads / null threadId). Fill-first already follows
  // config.activeCodexAccountId, which the caller persists before invoking this.
  seedPoolRotationAccount(POOL_KEY_CODEX, accountId);
  for (const scope of new Set(Object.values(NATIVE_MODEL_QUOTA_SCOPES))) {
    if (isIndependentCodexQuotaScope(scope)) {
      seedPoolRotationAccount(codexPoolKeyForScope(scope), accountId);
      manualPreference.set(codexPoolKeyForScope(scope), accountId);
    }
  }
  // Quota avoidance is a preference, like the soft avoid dropped above, and an operator naming
  // this account has overruled it. The hard cooldown is the part that survives.
  const overrule = (health: CodexUpstreamHealth) => {
    const { quotaAvoidUntil: _avoid, ...retained } = preservedCooldownFields(health);
    return retained;
  };
  const current = getAccountHealth(accountId);
  if (current) {
    const retained = overrule(current);
    if (Object.keys(retained).length === 0) deleteAccountHealth(accountId);
    else setAccountHealth(accountId, { consecutiveFailures: 0, ...retained });
  }
  // A reset-derived refusal records its avoidance on the SCOPED map and returns before the
  // account-wide entry is written, so naming the account has to reach that map too. Stopping
  // at `upstreamHealth` — and returning early when it holds nothing — overruled nothing in
  // the case that produces the avoidance this function exists to overrule.
  for (const [scope, health] of [...(listScopedHealthEntries(accountId))]) {
    const retained = overrule(health);
    if (Object.keys(retained).length === 0) deleteScopedHealth(accountId, scope);
    else setScopedHealth(accountId, scope, { consecutiveFailures: 0, ...retained });
  }
}

/**
 * Operator released the selection instead of naming a new account: return routing to
 * automatic without steering. A release names no replacement, so bound threads keep
 * their captured account and quota-avoidance health stands — nothing new overrules it.
 * What still has to go is the steering a selection planted: the one-shot manual
 * preference (releasing account A must not keep blocking every other pick), the
 * and the placement seeds on the shared ring and each independent
 * quota-scope ring, which would otherwise force the next pick onto that same account.
 */
export function resetCodexRoutingForClearedSelection(): void {
  manualPreference.delete(POOL_KEY_CODEX);
  clearPoolRotationState(POOL_KEY_CODEX);
  for (const scope of new Set(Object.values(NATIVE_MODEL_QUOTA_SCOPES))) {
    if (isIndependentCodexQuotaScope(scope)) {
      clearPoolRotationState(codexPoolKeyForScope(scope));
      manualPreference.delete(codexPoolKeyForScope(scope));
    }
  }
}

/** Unspent operator selections, keyed by quota scope. */
const manualPreference = new Map<string, string>();

/** Spend a selection preference when a dispatch on that account succeeds. */
export function consumeManualPreference(accountId: string, poolKey: string): void {
  if (manualPreference.get(poolKey) === accountId) manualPreference.delete(poolKey);
}

/** Drop an account's preference in every scope when it leaves the eligible roster. */
export function forgetManualPreference(accountId: string): void {
  for (const [poolKey, preferred] of manualPreference) {
    if (preferred === accountId) manualPreference.delete(poolKey);
  }
}

/** An unspent manual selection can defer a different placement in the same scope. */
export function manualPreferenceBlocks(poolKey: string, accountId: string): boolean {
  const preferred = manualPreference.get(poolKey);
  return preferred !== undefined && preferred !== accountId;
}

/** The unspent operator selection for this pool scope, if any. Read-only. */
export function unspentManualPreference(poolKey: string): string | undefined {
  return manualPreference.get(poolKey);
}

export function getEffectiveActiveCodexAccountId(config: OcxConfig): string | undefined {
  return singleCodexAccountId(config);
}

/** Whether the signed-in account matches the explicit operator pin. */
export function isEffectiveCodexAccountPinned(config: OcxConfig): boolean {
  const pinned = pinnedCodexAccountId(config);
  return pinned !== undefined && pinned === getEffectiveActiveCodexAccountId(config);
}

export function clearAllManualPreferences(): void {
  manualPreference.clear();
}

export function forgetRoutingPreferencesOutside(codexAccountIds: ReadonlySet<string>): void {
  for (const [poolKey, preferred] of manualPreference) {
    if (codexAccountIds.has(preferred)) continue;
    manualPreference.delete(poolKey);
  }
}
