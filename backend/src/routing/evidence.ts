import type { OcxConfig, OcxProviderConfig } from "../types";
import { candidateCapabilityEvidence } from "./capability";
import { costEvidenceForCandidate } from "./cost";
import type { PolicyCandidateEvidence } from "./evaluator";
import { policyCandidateHealthEvidence } from "./health";
import type { NormalizedRoutingProfile } from "./profile";
import { quotaEvidenceForCandidate } from "./quota";

export type RoutedProviderResolver = (
  providerName: string,
  provider: OcxProviderConfig,
) => OcxProviderConfig;

export function assemblePolicyCandidateEvidence(
  config: OcxConfig,
  profile: NormalizedRoutingProfile,
  now: number,
  options: { routedProviderConfig: RoutedProviderResolver },
): PolicyCandidateEvidence[] {
  return profile.candidates.map(candidate => {
    const provider = config.providers[candidate.provider];
    let routed: OcxProviderConfig | undefined;
    let routeResolutionFailed = !provider || provider.disabled === true;
    if (provider && provider.disabled !== true) {
      try {
        routed = options.routedProviderConfig(candidate.provider, provider);
      } catch {
        // This is known unavailability, not unknown capability evidence. Keep
        // the failure separate so permissive unknown policies cannot select it.
        routeResolutionFailed = true;
      }
    }

    return {
      provider: candidate.provider,
      model: candidate.model,
      ...(routeResolutionFailed ? { routeResolutionFailed: true } : {}),
      capability: routed
        ? candidateCapabilityEvidence(config, candidate.provider, candidate.model, routed)
        : undefined,
      health: policyCandidateHealthEvidence(config, candidate, now),
      quota: quotaEvidenceForCandidate({
        provider: candidate.provider,
        model: candidate.model,
      }, config),
      cost: costEvidenceForCandidate({
        provider: candidate.provider,
        model: candidate.model,
        limitUsd: profile.limits.maxEstimatedCostUsd,
      }),
    };
  });
}
