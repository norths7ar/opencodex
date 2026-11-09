import { isCodexAccountHeldForCredits } from "../account-credit-use";
import { isCodexAccountPaused } from "../account-pause";
import { isSelectableCodexPoolAccount } from "../account-id";
import { isCodexAccountUsable, type CodexAccountUsabilityOptions } from "../account-usability";
import { isCodexPoolRefreshCooling } from "../pool-refresh-backoff";
import { getAccountQuota } from "../quota";
import { codexPlanKey } from "../plan";
import { MAIN_CODEX_ACCOUNT_ID, getMainAccountPlan } from "../main-account";
import type { OcxConfig } from "../../types";
import {
  getCodexQuotaHealthSnapshot,
  isCodexAccountSoftAvoided,
  isCodexQuotaAvoided,
  type CodexQuotaScope,
} from "./health-store";

/** Normalized plan keys excluded by the channel’s configured policy. */
function excludedCodexPoolPlanKeys(config: OcxConfig): ReadonlySet<string> | undefined {
  const configured = config.codexPool?.excludedPlans;
  if (!configured?.length) return undefined;
  const keys = configured
    .map(plan => codexPlanKey(plan))
    .filter((key): key is string => key !== undefined);
  return keys.length > 0 ? new Set(keys) : undefined;
}

/** Whether the configured plan policy excludes this stored credential. */
export function isCodexAccountPlanExcluded(
  config: OcxConfig,
  accountId: string,
): boolean {
  if (accountId === MAIN_CODEX_ACCOUNT_ID) return false;
  const excluded = excludedCodexPoolPlanKeys(config);
  if (!excluded) return false;
  const plan = codexPlanKey(getPoolAccountPlan(config, accountId));
  return plan !== undefined && excluded.has(plan);
}

/** Whether the signed-in account has reached a usage limit with credits disabled. */
function isCodexAccountHeldAtUsageLimit(
  config: OcxConfig,
  accountId: string,
  now: number,
  selectionOptions?: CodexAccountUsabilityOptions,
): boolean {
  return isCodexAccountHeldForCredits(
    config,
    accountId,
    getAccountQuota(accountId),
    getPoolAccountPlanForSelection(config, accountId, selectionOptions),
    now,
  );
}

export function isCodexAccountSelectable(
  config: OcxConfig,
  accountId: string,
  now: number,
  quotaScope?: CodexQuotaScope,
  selectionOptions?: CodexAccountUsabilityOptions,
): boolean {
  return !isCodexAccountPaused(config, accountId)
    && !isCodexAccountPlanExcluded(config, accountId)
    && !isCodexAccountHeldAtUsageLimit(config, accountId, now, selectionOptions)
    && getCodexQuotaHealthSnapshot(accountId, quotaScope, now) === null
    && !isCodexQuotaAvoided(accountId, quotaScope, now)
    && !isCodexAccountSoftAvoided(accountId, now)
    && !isCodexPoolRefreshCooling(accountId, now)
    && isCodexAccountUsable(config, accountId, selectionOptions);
}

export function getPoolAccountPlan(config: OcxConfig, accountId: string): string | undefined {
  if (accountId === MAIN_CODEX_ACCOUNT_ID) return getMainAccountPlan();
  return (config.codexAccounts ?? [])
    .find(account => isSelectableCodexPoolAccount(account) && account.id === accountId)?.plan;
}

/**
 * Selection-only main routing must not lazily read the fenced native credential for its plan, and
 * neither may a request whose main candidacy comes from its own bearer (#5019): that request is
 * forbidden to read the physical main credential, so main is ranked without a plan.
 */
function getPoolAccountPlanForSelection(
  config: OcxConfig,
  accountId: string,
  selectionOptions?: CodexAccountUsabilityOptions,
): string | undefined {
  if (
    accountId === MAIN_CODEX_ACCOUNT_ID
    && (selectionOptions?.nativeMainSelectionOnly === true
      || selectionOptions?.requestOwnedMainCredential === true)
  ) {
    return undefined;
  }
  return getPoolAccountPlan(config, accountId);
}
