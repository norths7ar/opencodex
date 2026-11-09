/**
 * OCX-11 fallback policy (`fallback` in config.json). Every leaf is optional and absence keeps
 * today's behavior: model and provider fallback allowed, local fallback not granted.
 *
 * Contract and precedence: `src/routing/fallback-policy.ts` and `structure/fallback-policy.md`.
 */

/** A fallback kind this policy governs. Intra-pool account or key rotation is not one of them. */
export type OcxFallbackKind = "model" | "provider";

export interface OcxFallbackSwitches {
  /** false refuses a hop to another model of the same provider. */
  model?: boolean;
  /** false refuses a hop to another provider. */
  provider?: boolean;
}

export interface OcxFallbackConfig extends OcxFallbackSwitches {
  /**
   * Keyed by the provider a hop leaves: the resolved provider of the target that just failed or
   * is known unavailable. In combo [a, b, c] with providers.b.provider false, a to b is allowed and
   * b to c is refused.
   */
  providers?: Record<string, OcxFallbackSwitches>;
  /** Keyed by an exact public selector: policy/<id>, provider/model or an alias. */
  routes?: Record<string, OcxFallbackSwitches>;
  /** Keyed by a Remote Link id (lnk_…) or a Hub-client data key id. Read on the Home. */
  devices?: Record<string, OcxFallbackSwitches>;
}
