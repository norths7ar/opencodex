import type { OcxConfig } from "../../types";
import { isBareOpenAiFamilyModel, previewRouteModel, routeConcreteModel } from "../../router";
import { resolveChosenTarget } from "./shadow-target-availability";
import { getRoutingProfile, resolvePolicyProfileId } from "../../routing/profile";
import { NATIVE_RESERVE_MODEL } from "../../codex/catalog/native-models";
import {
  getMainAccountHardLockStatus,
  resolveMainAccountHardLockFallback,
} from "../../codex/main-account-hard-lock";
import { codexQuotaScopeForModel } from "../../codex/routing/health-store";
import { previewCodexAccountForRequest } from "../../codex/routing";
import { OPENAI_CODEX_PROVIDER_ID, isCanonicalOpenAiForwardProvider } from "../../providers/openai-tiers";
import { providerCodexAccountMode } from "../../providers/registry";
import {
  FallbackRefusedError, fallbackDeviceIds, fallbackHopKind, guardFallbackHop, resolveFallbackPolicy,
  type FallbackAdmission, type FallbackHop, type FallbackTarget,
} from "../../routing/fallback-policy";
/** Resolves to a synchronous ownership proof, or null when the caller does not own the locked main. */
export type MainHardLockCallerCheck = (sourceModel: string) => Promise<(() => boolean) | null>;

function targetsBlockedCanonicalOpenAi(config: OcxConfig, modelId: string): boolean {
  if (isBareOpenAiFamilyModel(modelId)) return true;
  const slash = modelId.indexOf("/");
  if (slash <= 0) return false;
  const providerName = modelId.slice(0, slash);
  const rest = modelId.slice(slash + 1);
  if (providerName !== OPENAI_CODEX_PROVIDER_ID || !isBareOpenAiFamilyModel(rest)) return false;
  const provider = config.providers?.[OPENAI_CODEX_PROVIDER_ID];
  return !provider || isCanonicalOpenAiForwardProvider({
    ...provider,
    ...(provider.authMode === undefined ? { authMode: "forward" as const } : {}),
  });
}

/**
 * What one rewritten request carries so the log can show the model the client asked for
 * while the pipeline routes the fallback. The rest of the pipeline sees an ordinary request
 * for the target selector.
 */
export interface MainHardLockFallbackOverride {
  sourceModel: string;
  targetModel: string;
}

function rawModelOf(body: unknown): { raw: Record<string, unknown>; model: string } | undefined {
  if (!body || typeof body !== "object" || Array.isArray(body)) return undefined;
  const raw = body as Record<string, unknown>;
  return typeof raw.model === "string" && raw.model.trim().length > 0
    ? { raw, model: raw.model }
    : undefined;
}

/**
 * Extended main-account hard-lock settings: when the lock blocks and no Codex account can
 * serve the request, rewrite it to the configured fallback model instead of letting the
 * dispatch refuse.
 *
 * Every guard bounds the rewrite to exactly the traffic the lock would otherwise fail, so
 * the setting can never widen what the lock protects:
 *
 * - The lock must be enabled AND reading `blocked` right now — `ready`, `unknown` and the
 *   explicit opt-out all admit, and the release is this same observation, so the reroute
 *   ends on its own the moment the blocking windows drop below their thresholds.
 * - Only the channel’s signed-in account can keep a subscription request on Codex. Inactive
 *   stored credentials do not suppress the fallback.
 * - Only bare Codex-native selectors qualify. Provider-qualified rows, account
 *   namespaces and policy routes name their own destination on purpose; rewriting those
 *   would override an explicit choice, so they keep the lock's refusal.
 * - A Codex-native fallback target is refused outright: rerouting gpt-* to gpt-* would
 *   re-enter the same refusal, and the honest behavior is the refusal, not a loop.
 * - Luna Reserve keeps its own admission policy (`assertReserveAdmission`); its helper
 *   traffic is not operator chat and must not silently change models.
 */
export async function applyMainHardLockFallbackOverride(
  body: unknown,
  config: OcxConfig,
  now?: number,
  callerCheck?: MainHardLockCallerCheck,
  admission?: FallbackAdmission,
): Promise<MainHardLockFallbackOverride | null> {
  const parsed = rawModelOf(body);
  if (!parsed) return null;
  const fallback = resolveMainAccountHardLockFallback(config);
  if (!fallback) return null;
  if (targetsBlockedCanonicalOpenAi(config, fallback.model)) return null;
  if (!isBareOpenAiFamilyModel(parsed.model)
    || parsed.model === NATIVE_RESERVE_MODEL ) return null;
  const accountMode = providerCodexAccountMode(
    OPENAI_CODEX_PROVIDER_ID,
    config.providers?.[OPENAI_CODEX_PROVIDER_ID],
  );
  if (accountMode === "direct") {
    // Direct may forward a different caller's healthy credential. Only stored-main substitution
    // or an observed-main match owes this lock. Recheck usage after the startup binding wait.
    const stillOwnsLockedMain = callerCheck ? await callerCheck(parsed.model) : null;
    // Everything from here to the rewrite is synchronous, so this proof cannot go stale before it.
    if (!stillOwnsLockedMain?.()) return null;
    if (getMainAccountHardLockStatus(config, now ?? Date.now()).state !== "blocked") return null;
  } else {
    if (getMainAccountHardLockStatus(config, now ?? Date.now()).state !== "blocked") return null;
    if (previewCodexAccountForRequest(null, config, now ?? Date.now(), codexQuotaScopeForModel(parsed.model)) !== null) return null;
  }

  // Policies keep their own dispatch admission and declared failover/evidence.
  // Other chosen targets must resolve now; a removed qualifier cannot become a default send.
  const policyId = resolvePolicyProfileId(config, fallback.model);
  const profile = policyId ? getRoutingProfile(config, policyId) : undefined;
  if (profile) {
    // Match the actual first guard's selector order. This neither picks nor filters a target.
    const denied = allDeclaredTargetsDenied(config, profile.candidates, {
      site: "hard-lock",
      route: parsed.model,
      aliases: [`policy/${policyId}`],
      from: { provider: OPENAI_CODEX_PROVIDER_ID, model: parsed.model },
      trigger: "hard_lock", admission, ...(now !== undefined ? { now } : {}),
    });
    if (denied) {
      // One refusal witness for an all-denied declaration, never a speculative taken event.
      const hop = guardFallbackHop(config, denied);
      if (!hop.ok) throw new FallbackRefusedError(hop.response);
    }
    // Mixed or unresolved declarations grant nothing: actual first/retry guards still decide.
    // A selector rewrite is not a selected hop and reserves no send or taken event here.
  } else {
    try {
      const target = resolveChosenTarget(fallback.model, model => previewRouteModel(config, model));
      if ("unavailable" in target) return null;
    } catch {
      // An unavailable target keeps the source lock's refusal without touching the body.
      return null;
    }
    // Concrete targets keep their existing proactive admission at body rewrite.
    const hop = guardFallbackHop(config, {
      site: "hard-lock",
      route: parsed.model,
      from: { provider: OPENAI_CODEX_PROVIDER_ID, model: parsed.model },
      to: hardLockTargetOf(config, fallback.model),
      trigger: "hard_lock", admission, ...(now !== undefined ? { now } : {}),
    });
    if (!hop.ok) throw new FallbackRefusedError(hop.response);
    hop.commit();
  }

  parsed.raw.model = fallback.model;
  if (fallback.reasoningEffort !== undefined) {
    const reasoning = parsed.raw.reasoning;
    parsed.raw.reasoning = {
      ...(reasoning && typeof reasoning === "object" && !Array.isArray(reasoning)
        ? reasoning as Record<string, unknown>
        : {}),
      effort: fallback.reasoningEffort,
    };
  }
  return { sourceModel: parsed.model, targetModel: fallback.model };
}

/**
 * Pure policy feasibility over declared concrete destinations. Unknown resolution is not denial.
 * No picker, history, cooldown, event or send-budget operation runs for individual candidates.
 * The first denied hop is only a refusal witness once every declaration is known denied.
 */
function allDeclaredTargetsDenied(
  config: OcxConfig, targets: readonly FallbackTarget[], hop: Omit<FallbackHop, "to">,
): FallbackHop | undefined {
  const deviceIds = fallbackDeviceIds(config, hop.admission);
  const routes = [hop.route, ...(hop.aliases ?? [])];
  let witness: FallbackHop | undefined;
  for (const target of targets) {
    let to: FallbackTarget;
    try {
      const route = routeConcreteModel(config, `${target.provider}/${target.model}`);
      to = { provider: route.providerName, model: route.modelId };
    } catch { return undefined; }
    const kind = fallbackHopKind(hop.from, to);
    if (kind === null || resolveFallbackPolicy(config, { kind, routes, provider: hop.from.provider, deviceIds }).allowed) return undefined;
    witness ??= { ...hop, to };
  }
  return witness;
}

/** Concrete and account-qualified targets retain the existing config-only resolution. */
function hardLockTargetOf(config: OcxConfig, model: string): FallbackTarget {
  try {
    const route = previewRouteModel(config, model);
    return { provider: route.providerName, model: route.modelId };
  } catch {
    const slash = model.indexOf("/");
    return slash > 0 ? { provider: model.slice(0, slash), model: model.slice(slash + 1) } : { provider: "", model };
  }
}
