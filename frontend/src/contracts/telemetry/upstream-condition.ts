export const UPSTREAM_CONDITIONS = [
  "auth_failed",
  "access_denied",
  "context_overflow",
  "location_unsupported",
  "account_verification_required",
  "billing_exhausted",
  "model_unavailable",
  "rate_limited",
  "upstream_unavailable",
  "upstream_failed",
  "request_rejected",
] as const;

export type UpstreamCondition = (typeof UPSTREAM_CONDITIONS)[number];
