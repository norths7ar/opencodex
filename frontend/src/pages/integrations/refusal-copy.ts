import type { TKey } from "../../i18n/shared";
type Translate = (key: TKey, vars?: Record<string, string>) => string;
const LOCALIZED_REASONS: ReadonlySet<string> = new Set(["non_loopback"]);
function reasonKey(reason: string | undefined): TKey {
  if (reason === "conflict") return "integrations.error.conflict";
  if (reason === "unsafe") return "integrations.error.unsafe";
  if (reason === "non_loopback") return "integrations.error.nonLoopback";
  return "integrations.error.generic";
}

export function describeIntegrationRefusalParts(
  t: Translate,
  refusal: {
    reason?: string;
    message?: string;
    clientId: string;
    snapshotPath?: string;
    residual?: boolean;
  },
): string {
  const message = LOCALIZED_REASONS.has(refusal.reason ?? "")
    ? t(reasonKey(refusal.reason), { client: refusal.clientId })
    : refusal.message || t(reasonKey(refusal.reason));
  if (refusal.snapshotPath) {
    // A dead end the user cannot finish by hand is worse than no rollback at
    // all, so the path rides along whenever the server sent one.
    return t(refusal.residual ? "integrations.error.residual" : "integrations.error.recover", {
      message,
      path: refusal.snapshotPath,
    });
  }
  if (refusal.residual) return t("integrations.error.residualNoSnapshot", { message });
  return refusal.reason === "conflict" || refusal.reason === "unsafe"
    ? `${t(reasonKey(refusal.reason))} ${message}`
    : message;
}
