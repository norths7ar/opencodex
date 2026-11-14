import type { TKey } from "./i18n/shared";

export interface StartupRiskDetail {
  diagnosticStale?: boolean;
  routingKind: "native" | "opencodex-local" | "custom-local" | "custom-remote" | "unknown";
  shimCoverage: "full" | "cli-only" | "none";
}

/** Presentation is distinct from conservative restart-protection credit. */
export function startupPresentation(
  data: {
    status: "native" | "protected" | "at-risk";
    routingKind?: unknown;
    diagnosticStale?: unknown;
  },
  failed = false,
): "native" | "protected" | "at-risk" | "unverified" | "error" {
  if (
    !failed &&
    data.diagnosticStale !== true &&
    (data.status === "native" || data.status === "protected")
  )
    return data.status;
  if (data.diagnosticStale === true && data.routingKind === "opencodex-local") return "unverified";
  return failed ? "error" : data.status;
}

export function startupChecklistDone(data: {
  rebootSafe?: boolean;
  status?: "native" | "protected" | "at-risk";
  routingKind?: unknown;
  diagnosticStale?: unknown;
  protection?: string;
  serviceInstalled?: boolean;
  shimInstalled?: boolean;
}): boolean | undefined {
  if (data.diagnosticStale === true) return undefined;
  if (typeof data.rebootSafe === "boolean") return data.rebootSafe;
  if (data.status && startupPresentation({ ...data, status: data.status }) === "unverified")
    return undefined;
  return (
    (data.protection !== undefined && data.protection !== "none") ||
    Boolean(data.serviceInstalled || data.shimInstalled)
  );
}

export function startupRiskDetailKey(health: StartupRiskDetail): TKey {
  if (health.routingKind === "custom-local") return "startup.riskDetailCustomLocal";
  if (health.shimCoverage === "cli-only") return "startup.riskDetailWindowsShim";
  return "startup.riskDetail";
}

export interface SettingsPollEpoch {
  request: number;
  mutation: number;
}

export function settingsPollMayCommit(
  started: SettingsPollEpoch,
  current: SettingsPollEpoch & { mutationInFlight: boolean },
): boolean {
  return (
    !current.mutationInFlight &&
    started.request === current.request &&
    started.mutation === current.mutation
  );
}

/** Snapshot + bump one request epoch before issuing a poll fetch. */
export function beginPollEpoch(
  request: { current: number },
  mutation: { current: number },
): SettingsPollEpoch {
  return {
    request: ++request.current,
    mutation: mutation.current,
  };
}

/** Snapshot + bump request epochs before issuing paired settings/shadow poll fetches. */
export function beginPollEpochs(refs: {
  settingsRequest: { current: number };
  settingsMutation: { current: number };
  shadowRequest: { current: number };
  shadowMutation: { current: number };
}): {
  settings: SettingsPollEpoch;
  shadow: SettingsPollEpoch;
} {
  return {
    settings: beginPollEpoch(refs.settingsRequest, refs.settingsMutation),
    shadow: beginPollEpoch(refs.shadowRequest, refs.shadowMutation),
  };
}

export type StartupHealthStatus = "native" | "protected" | "at-risk" | "unverified" | "error";

/**
 * Probe result for the dashboard chip: the status plus whether the server answered from
 * its stale-while-revalidate fallback.
 *
 * The status alone is not enough. `/api/startup-health` answers immediately from a 30s
 * cache and kicks the real probe off in the background, so the first response after a
 * cold start (or after the TTL expires) is a conservative placeholder. A consumer that
 * only reads the status has no way to know it should look again soon, and on the
 * dashboard that meant the chip sat on the placeholder until the next 30s poll — which
 * is why clicking "refresh quota" (any action that re-mounted the chip) appeared to be
 * what fixed it.
 */
export interface StartupHealthProbe {
  status: StartupHealthStatus;
  /** True while the server is still resolving the real answer in the background. */
  stale: boolean;
}

/** How soon to re-ask after a stale answer; matches the Startup page's follow-up delay. */
export const STARTUP_HEALTH_STALE_RETRY_MS = 2_000;

/**
 * Map a startup-health API payload to the dashboard chip status.
 *
 * `diagnosticStale` means the server is still refreshing (SWR fallback / expired TTL).
 * That is NOT a hard read failure — collapsing it to "error" shows the misleading
 * "Could not read startup protection" chip whenever the 30s cache misses.
 * Preserve the backend verdict for unrelated routing; an unresolved local-service probe
 * is shown as unverified. Reserve "error" for hard failures without prior context.
 */
export function mapStartupHealthProbe(data: {
  status?: unknown;
  diagnosticStale?: unknown;
  routingKind?: unknown;
}): StartupHealthStatus | null {
  const status = data.status;
  const valid = status === "native" || status === "protected" || status === "at-risk";
  if (!valid) return null;
  return startupPresentation({ ...data, status });
}

/** True when a probe result should be re-fetched shortly instead of waiting for the poll. */
export function probeNeedsFastRetry(probe: StartupHealthProbe | undefined | null): boolean {
  if (!probe) return false;
  // A hard error is retried by the normal poll; only an in-progress server refresh
  // resolves on its own within seconds.
  return probe.stale && probe.status !== "error";
}

/**
 * Settings may only seed startup health while it is still unknown or a hard error.
 * After `/api/startup-health` has produced a real status, it stays authoritative.
 * A stale local-routing seed is unverified; other stale seeds leave the previous value alone
 * so a synthetic not-installed placeholder cannot flash an alarm for a healthy service.
 */
export function seedStartupHealthFromSettings(
  previous: StartupHealthStatus | null,
  seeded:
    | {
        status: "native" | "protected" | "at-risk";
        diagnosticStale: boolean;
        routingKind?: unknown;
      }
    | null
    | undefined,
  authoritative = previous !== null && previous !== "error",
): StartupHealthStatus | null {
  if (!seeded) return previous;
  if (authoritative) return previous;
  if (seeded.diagnosticStale)
    return seeded.routingKind === "opencodex-local" ? "unverified" : previous;
  return startupPresentation(seeded);
}

/** Single owner cadence for project-config diagnostics (ms). */
export const PROJECT_CONFIG_DIAGNOSTICS_POLL_MS = 30_000;
