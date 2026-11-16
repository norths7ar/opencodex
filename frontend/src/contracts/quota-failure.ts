/** Closed account-probe diagnoses; never upstream text, URLs, credentials or routing policy. */
export const QUOTA_FAILURE_CODES = [
  "account_unavailable",
  "access_denied",
  "rate_limited",
  "upstream_error",
  "redirect_blocked",
  "destination_blocked",
  "dns_failed",
  "timeout",
  "transport_error",
  "response_unusable",
] as const;
export type QuotaFailureCode = (typeof QUOTA_FAILURE_CODES)[number];
export function parseQuotaFailureCode(value: unknown): QuotaFailureCode | undefined {
  return QUOTA_FAILURE_CODES.find((code) => code === value);
}
