import type { OcxConfig } from "../types";
import { getCachedProviderRoutingQuota } from "./quota-routing-cache";
import type { ProviderQuota, ProviderQuotaWindow } from "./quota-types";

function quotaWindowExhausted(percent: number | undefined, resetAt: number | undefined, now: number): boolean {
  if (typeof percent !== "number" || !Number.isFinite(percent) || percent < 100) return false;
  return typeof resetAt !== "number" || !Number.isFinite(resetAt) || resetAt > now;
}

// `customWindows` is a generic carrier. Most labels are provider-wide counters ("Prepaid
// credits", "Free trial", "burst", "Spark") and must gate every model. Anthropic also rides it
// for per-model family counters, where a spent Opus week says nothing about a Sonnet request.
// `routing/quota.ts` already scopes those per model when it ranks accounts
// (`anthropicFamilyWindow`); catalog quota hints use the same scope.
//
// The scoping keys on `window.scope`, which the PRODUCER sets only where it proved model scope
// structurally, NEVER on the label text: `quota/antigravity.ts` forwards an upstream
// `group.displayName` unchanged, so a provider-wide group named "Opus" read as per-model would
// leave a spent window unenforced and send a doomed request. Every unproven direction keeps
// gating everything, which is the behaviour before this change: no scope on the window, no
// model on the request, or a family this gateway cannot match against a model id. Failing
// closed there costs at most a diverted target.
const MODEL_FAMILY_WINDOW_LABELS = new Set(["fable", "opus", "sonnet"]);

function customWindowAppliesToModel(window: ProviderQuotaWindow, model: string | undefined): boolean {
  // Exact Antigravity model readings are diagnostic only. They are not permission or
  // provider-wide exhaustion evidence until a separate eligibility contract consumes them.
  if (window.modelId !== undefined) return false;
  if (window.scope !== "model" || model === undefined) return true;
  const family = window.label.trim().toLowerCase();
  if (!MODEL_FAMILY_WINDOW_LABELS.has(family)) return true;
  return model.toLowerCase().includes(family);
}

export function cachedProviderQuotaIsExhausted(
  quota: ProviderQuota | null,
  now = Date.now(),
  model?: string,
): boolean {
  if (!quota) return false;
  if (quotaWindowExhausted(quota.fiveHourPercent, quota.fiveHourResetAt, now)) return true;
  if (quotaWindowExhausted(quota.weeklyPercent, quota.weeklyResetAt, now)) return true;
  if (quotaWindowExhausted(quota.monthlyPercent, quota.monthlyResetAt, now)) return true;
  if (quota.customWindows?.some(window => customWindowAppliesToModel(window, model)
    && quotaWindowExhausted(window.percent, window.resetAt, now))) return true;
  if (quota.creditsUsd?.unlimited !== true
      && typeof quota.creditsUsd?.percent === "number"
      && Number.isFinite(quota.creditsUsd.percent)
      && quota.creditsUsd.percent >= 100
      && quota.creditsUsd.remaining <= 0) return true;
  return false;
}

/**
 * Why a catalog row is offered but cannot currently serve a request (#1711).
 *
 * Only one reason exists today. It is a string rather than a boolean so a later cause — a
 * cooldown, a revoked key — can be told apart by a consumer that already reads the field.
 */
export type QuotaInactiveReason = "no_credit";

/**
 * `"no_credit"` when every USABLE target of a catalog row has positive exhaustion evidence
 * (#1711), otherwise undefined.
 *
 * This reuses the credential-scoped evidence and exhaustion rules used by runtime selection.
 * Display-only account, model-group and service windows cannot mark a whole provider inactive.
 *
 * Three rules carry the correctness, all inherited rather than restated:
 *
 * - A target the operator has removed or disabled is not usable and is not evidence either way;
 *   it drops out before the vote. If nothing is left, the row is unavailable for an operator
 *   reason rather than a quota one, so this returns undefined.
 * - The canonical ChatGPT forward provider is exempt. Native account selection owns model-scoped
 *   quota, and a provider-level summary cannot veto it.
 * - A stale cache is NOT exhaustion. `getCachedProviderRoutingQuota` returns null past its 30-minute
 *   window, and a null reading ends the vote rather than counting as evidence, so an unprobed
 *   provider is never marked inactive.
 *
 * "Every" is the bar on purpose: one target that can still serve makes the row serviceable.
 */
export function quotaInactiveReason(
  config: OcxConfig,
  targets: readonly { provider: string; model?: string }[],
  now = Date.now(),
): QuotaInactiveReason | undefined {
  const usable = targets.filter(target => {
    if (!Object.hasOwn(config.providers, target.provider)) return false;
    const provider = config.providers[target.provider];
    return !!provider && provider.disabled !== true;
  });
  if (usable.length === 0) return undefined;
  for (const target of usable) {
    const provider = config.providers[target.provider]!;
    const quota = getCachedProviderRoutingQuota(target.provider, provider, now);
    if (!quota || !cachedProviderQuotaIsExhausted(quota, now, target.model)) return undefined;
  }
  return "no_credit";
}

