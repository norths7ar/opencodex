import { appliedCodexSettingsDigest } from "../codex/applied-settings-digest";
import type { CodexMainAccountPolicyHealth } from "../oauth/health";
import { durableBunRuntime } from "../lib/bun-runtime";
import { existsSync, readFileSync } from "node:fs";
import { codexAutoStartEnabled, getConfigPath, readConfigDiagnostics } from "../config";
import { getPidPath, readPid, readRuntimePort, type RuntimePortState } from "../config/process-state";
import { diagnoseCodexBundledPlugins, type CodexPluginsDiagnostic } from "../codex/plugins-doctor";
import { findLiveProxy, probeHostname, type LiveProxy } from "../server/proxy-liveness";
import type { OcxConfig } from "../types";
import { diagnoseService, serviceLogPath } from "../service";
import { collectStartupHealth, startupHealthReadBudgetMs, type StartupHealth } from "../codex/autostart-health";
import { getCodexRoutingKind } from "../codex/inject";
import { missingOwnedCatalogPath } from "../codex/inject/config-toml";
import { CODEX_CONFIG_PATH } from "../codex/paths";
import { diagnoseCodexShim } from "../codex/shim";
import { displayCodexRuntimePath, effortClampAppliesToRuntime, liveRemovedEfforts, loadLastEffortClamp, resolveCodexRuntime } from "../codex/runtime";
import { packageVersion } from "./help";
import { computeVersionSkew, type VersionSkew } from "./version-skew";
import { redactSecretString, redactUserPath } from "../lib/redact";
import { collectOrcaCodexHomeDiagnostic, type OrcaCodexHomeDiagnostic } from "../codex/home";
import { grokFenceEndpointDrift, readGrokStatus } from "../grok/status";
import { effectiveLoopbackListenerPort } from "../codex/loopback-target";
import { journaledInjectedOpenaiBaseUrl, journaledInjectedRealtimeWsBaseUrl } from "../codex/journal";
import { detectCodexRoutingDrift, type JournaledCodexRouting } from "../codex/routing-drift";
import { claudeDesktopIntegrationEnabled } from "../codex/desired-state";
import { claudeDesktopPolicyHealth, probeClaudeDesktopPolicy, type ClaudeDesktopPolicyHealth } from "../claude/desktop-policy";
export { proxyHealthFailureReason, isConnectionRefused, isUncleanExitEvidence, probeUncleanExitState } from "./status-probes";
export type { ListenTarget } from "./status-probes";
import { checkProxyHealth, probeUncleanExitState, type ListenTarget } from "./status-probes";
import { LOCAL_MANAGEMENT_READ_PATHS } from "../lib/local-management-capability";
import { fetchBoundLocalManagementRead } from "../server/local-management-read-client";
import { collectLocalCredentials, type LocalCredentialSnapshot } from "../local-credentials/inventory";

export type CliStatusJson = {
  /**
   * This installation's stored provider credentials, counted passively, with an opaque generation
   * that changes on every credential publication. Omitted when the count cannot be proven complete
   * or no live runtime observes the stores; absence never means zero.
   */
  localCredentials?: LocalCredentialSnapshot;
  appliedCodexSettingsDigest: string;
  mainAccountHardLock?: CodexMainAccountPolicyHealth;
  /** OCX-11 fallback state; see src/cli/status-fallback.ts. */
  fallback?: import("./status-fallback").StatusFallback;
  schemaVersion: 1;
  proxy: {
    running: boolean;
    pid: number | null;
    /** Persisted owner records outlived their process: the last proxy did not exit cleanly. */
    staleProcessState: boolean;
    health: {
      ok: boolean;
      url: string;
      message: string;
    };
  };
  dashboard: { url: string };
  listen: {
    port: number;
    hostname: string | null;
    source: "runtime" | "config";
  };
  paths: {
    config: string;
    pid: string;
    runtime: string;
  };
  runtime: {
    source: string;
    overrideEnv?: string;
  };
  codexAutostart: boolean;
  startup: StartupHealth;
  defaultProvider: string | null;
  config: {
    source: "default" | "file" | "fallback";
    error: string | null;
  };
  service: { summary: string };
  codexShim: { summary: string };
  codexPlugins: CodexPluginsDiagnostic;
  codexRuntime: {
    path: string;
    version: string | null;
    source: string;
    newerAvailable: { path: string; version: string | null } | null;
    warning: string | null;
    catalogClamp: {
      active: boolean;
      removedEfforts: string[];
      runtimeVersion: string | null;
    };
  };
  codexHome: OrcaCodexHomeDiagnostic;
  claudeDesktop: {
    desiredEnabled: boolean;
    policy: ClaudeDesktopPolicyHealth;
  };
  /**
   * This CLI's version against the running proxy's (#2701).
   *
   * Additive and optional-by-value, so `schemaVersion` stays 1: an existing consumer that
   * ignores the key is unaffected, and `proxyVersion` is null when nothing is live.
   */
  versionSkew: VersionSkew;
  startupSource: "live" | "local";
};

export type CliStatusView = {
  json: CliStatusJson;
  proxyLabel: string;
  healthLabel: string;
};


type StatusListenConfig = Pick<OcxConfig, "port" | "hostname">;

function statusDashboardUrl(hostname: string | undefined, port: number): string {
  const reachableHostname = probeHostname(hostname);
  const dashboardHostname = reachableHostname === "127.0.0.1"
    || reachableHostname === "[::1]"
    || reachableHostname.toLowerCase() === "localhost"
    ? "localhost"
    : reachableHostname;
  return `http://${dashboardHostname}:${port}/`;
}

const STARTUP_HEALTH_BOOLEAN_FIELDS = [
  "routingInjected", "localRoutingDependency", "autostartEnabled", "rebootSafe",
  "serviceInstalled", "serviceViable", "serviceEnabled", "serviceRunning",
  "serviceStale", "serviceConflict", "shimInstalled", "shimHealthy",
  "serviceSupported", "diagnosticStale",
] as const;

export async function fetchLiveStartupHealth(
  live: NonNullable<Awaited<ReturnType<typeof findLiveProxy>>>,
  deps: Parameters<typeof fetchBoundLocalManagementRead>[2] = {},
): Promise<StartupHealth | null> {
  const result = await fetchBoundLocalManagementRead(
    live, LOCAL_MANAGEMENT_READ_PATHS.startupHealth, { timeoutMs: startupHealthReadBudgetMs(), ...deps, requireResponseProof: true },
  );
  if (result.kind !== "response" || !result.response.ok) return null;
  let payload: unknown;
  try { payload = await result.response.json(); } catch { return null; }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const row = payload as Record<string, unknown>;
  if (row.status !== "native" && row.status !== "protected" && row.status !== "at-risk") return null;
  if (row.protection !== "service" && row.protection !== "shim" && row.protection !== "none") return null;
  if (row.routingKind !== "native" && row.routingKind !== "opencodex-local"
    && row.routingKind !== "custom-local" && row.routingKind !== "custom-remote" && row.routingKind !== "unknown") return null;
  if (row.shimCoverage !== "full" && row.shimCoverage !== "cli-only" && row.shimCoverage !== "none") return null;
  if (typeof row.platform !== "string") return null;
  if (row.recommendedCommand !== null && typeof row.recommendedCommand !== "string") return null;
  if (!row.commands || typeof row.commands !== "object" || Array.isArray(row.commands)) return null;
  for (const key of ["installService", "repairService", "installShim", "restoreNative"] as const) {
    if (typeof (row.commands as Record<string, unknown>)[key] !== "string") return null;
  }
  if (row.routingAdoption !== undefined) {
    if (!row.routingAdoption || typeof row.routingAdoption !== "object" || Array.isArray(row.routingAdoption)) return null;
    const adoption = row.routingAdoption as Record<string, unknown>;
    if (adoption.adoption !== "not-applicable" && adoption.adoption !== "adopted"
      && adoption.adoption !== "pending-client-restart" && adoption.adoption !== "unknown") return null;
    if (adoption.injectedAtMs !== null && typeof adoption.injectedAtMs !== "number") return null;
    if (typeof adoption.observedClients !== "number" || !Array.isArray(adoption.staleClients)) return null;
    for (const client of adoption.staleClients) {
      if (!client || typeof client !== "object" || Array.isArray(client)) return null;
      const row = client as Record<string, unknown>;
      if (typeof row.pid !== "number" || typeof row.startedAtMs !== "number") return null;
    }
  }
  for (const key of STARTUP_HEALTH_BOOLEAN_FIELDS) if (typeof row[key] !== "boolean") return null;
  return payload as StartupHealth;
}

export function selectStatusStartupHealth(
  liveStartup: StartupHealth | null,
  fallback: () => StartupHealth,
): { startup: StartupHealth; startupSource: CliStatusJson["startupSource"] } {
  if (!liveStartup) return { startup: fallback(), startupSource: "local" };
  return { startup: liveStartup, startupSource: "live" };
}

/** Build the service summary from the same startup source that `ocx status` selected. */
export function statusServiceSummary(
  liveStartup: StartupHealth | null,
  service: Pick<ReturnType<typeof diagnoseService>, "installed" | "summary">,
  live: boolean,
  startupSource?: CliStatusJson["startupSource"],
): string {
  if (live && liveStartup) {
    if (liveStartup.protection === "service" && liveStartup.serviceViable) {
      return `running under the live managed service (logs: ${serviceLogPath()})`;
    }
    const state = [
      liveStartup.serviceInstalled ? "installed" : "absent",
      liveStartup.serviceRunning ? "running" : "not running",
      liveStartup.serviceViable ? "viable" : "not viable",
    ].join(", ");
    const action = liveStartup.recommendedCommand ? `; run '${liveStartup.recommendedCommand}'` : "";
    const label = (startupSource ?? (live ? "live" : "local")) === "live" ? "live" : "local";
    return `${label} startup reports service ${state}${action} (logs: ${serviceLogPath()})`;
  }
  return service.installed && !live
    ? `${service.summary} — registered but NOT serving; see ${serviceLogPath()} and re-run 'ocx service repair'`
    : service.summary;
}

export function selectListenTarget(
  config: StatusListenConfig,
  pid: number | null,
  runtimePort: RuntimePortState | null,
): ListenTarget {
  const currentRuntimePort = pid && runtimePort?.pid === pid ? runtimePort : null;
  const port = currentRuntimePort ? currentRuntimePort.port : config.port ?? 10100;
  const hostname = currentRuntimePort?.hostname ?? config.hostname;
  return {
    port,
    hostname,
    source: currentRuntimePort ? "runtime" : "config",
    healthUrl: `http://${probeHostname(hostname)}:${port}/healthz`,
    dashboardUrl: statusDashboardUrl(hostname, port),
  };
}

/** Prefer live result (including authoritative null pid) over the on-disk pid file. */
export function resolveStatusPid(
  live: { pid: number | null } | null,
  pidFile: number | null,
): number | null {
  return live ? live.pid : pidFile;
}

/**
 * `ocx status` greens on process liveness alone, so a proxy that answers
 * /healthz reads healthy even when Codex is not pointed at it and every routed
 * request goes to OpenAI instead (#2411). The proxy line is not wrong — the
 * listener really is up — so it keeps its check, and this supplies the signal
 * that was missing rather than corrupting the one that was already honest.
 *
 * Only `native` warns. `custom-local` and `unknown` are also "this proxy is
 * unused", but startupHealthSummary already renders both as AT RISK with a
 * remedy command, and `custom-remote` is a deliberate operator choice. Warning
 * on all four would teach operators to skip the line that matters.
 *
 */
export function unusedProxyWarningLines(input: {
  proxyUp: boolean;
  routingKind: StartupHealth["routingKind"];
}): string[] {
  if (input.routingKind !== "native") return [];
  if (!input.proxyUp) return [];
  return [
    "⚠️  Codex routing is native — the running proxy is unused.",
    "   Codex requests go to OpenAI, not this proxy. Re-point with: ocx start",
  ];
}

/**
 * The mirror case: routing is ours and nothing is answering it.
 *
 * #5261: this state does not merely fail model calls. The root `openai_base_url` we inject
 * is the base URL of Codex's own built-in openai provider, so with the proxy down a user can
 * be stopped at Codex sign-in with no mention of opencodex anywhere on the screen. The
 * injection is on disk and survives reboot, so it does not clear itself.
 *
 * The rest of the not-running report offers only ways to bring the proxy BACK, which is the
 * wrong half of the choice for someone who wants their editor working again now. `ocx restore`
 * needs no proxy, no management API and no network, so name it here — this report is the
 * surface such a user is most likely to reach before the config file itself.
 *
 * Restricted to routing opencodex owns. `custom-local` is somebody else's gateway, and
 * `ocx restore` would not remove it.
 */
export function deadProxyRoutingAdviceLines(input: {
  proxyUp: boolean;
  routingKind: StartupHealth["routingKind"];
  /** #7563: on a connected client nothing but `ocx sync` writes the routing back after a restore. */
}): string[] {
  if (input.proxyUp || input.routingKind !== "opencodex-local") return [];
  return [
    "Codex is still pointed at this proxy, so sign-in and model requests both fail while it is down.",
    "To hand Codex back to its own account and endpoints without starting anything: ocx restore",
  ];
}

type StatusRoutingView = Pick<CliStatusJson, "proxy" | "startup">;

export function statusProxyUp(json: StatusRoutingView): boolean {
  return Boolean(json.proxy.pid || json.proxy.health.ok);
}

/** `unusedProxyWarningLines` for the human `ocx status` report. */
export function statusUnusedProxyLines(json: StatusRoutingView): string[] {
  return unusedProxyWarningLines({
    proxyUp: statusProxyUp(json),
    routingKind: json.startup.routingKind,
  });
}

/** `deadProxyRoutingAdviceLines` for the not-running half of the human `ocx status` report. */
export function statusDeadProxyLines(json: StatusRoutingView): string[] {
  return [
    json.startup.routingKind === "native"
      ? "↳ Not running — Claude requests will fail with connection errors."
      : "↳ Not running — Codex/Claude requests will fail with connection errors.",
    ...deadProxyRoutingAdviceLines({
      proxyUp: false,
      routingKind: json.startup.routingKind,
    }),
  ];
}

/**
 * Read the live Codex config and report an opencodex catalog pointer whose file is gone.
 *
 * Unreadable or absent config is reported as no finding rather than as a problem: this is a
 * diagnostic line, and inventing one from missing evidence is worse than staying quiet.
 */
export function detectMissingCodexCatalogPath(): string | null {
  try {
    if (!existsSync(CODEX_CONFIG_PATH)) return null;
    return missingOwnedCatalogPath(readFileSync(CODEX_CONFIG_PATH, "utf8"));
  } catch {
    return null;
  }
}

/**
 * Owned Codex routing that names a local port the live proxy does not serve.
 *
 * `classifyCodexRouting` calls that "opencodex-local" whatever the port, so without this line the
 * state behind a killed second instance was invisible here. Detection is `routing-drift.ts`, the
 * same rule the owner's healer uses; this only reports and reads the journal without side effects.
 * A sibling's report says nothing: its routing correctly names the owner it runs beside. Whether a
 * healer runs, or is gated or paused, is not visible from here, so the line promises only `ocx sync`.
 */
export function codexRoutingDriftWarning(input: {
  content: string | null;
  livePort: number | undefined;
  loopbackPort: number | null;
  /** `siblingOfPort` from this home's runtime record. */
  siblingOfPort: number | undefined;
  journaled: () => JournaledCodexRouting;
}): string | null {
  const { content, livePort, loopbackPort } = input;
  if (content === null || livePort === undefined || input.siblingOfPort !== undefined) return null;
  const drift = detectCodexRoutingDrift(content, {
    ownPorts: loopbackPort === null ? [livePort] : [livePort, loopbackPort],
    journaled: input.journaled,
  });
  if (drift.kind !== "foreign") return null;
  const ports = [...new Set(drift.targets.map(target => target.port))].join(", ");
  return `Codex routing points at port ${ports}, but the proxy is on ${livePort}; if nothing serves that port, `
    + "new Codex threads fail with \"Connection refused\". Run 'ocx sync' to re-point it now; a running "
    + "owner proxy may also do that on its own once the port stays dead.";
}

/**
 * The one state in this report where Codex is broken independently of the proxy (#5261).
 *
 * A `model_catalog_json` naming a file that is gone stops Codex loading its configuration at
 * all, so it presents as the same blank wall as dead routing while having a different cause and
 * a different fix. Both are named, because restarting the proxy rewrites the catalog and
 * restoring removes the pointer, and which one the user wants is their choice, not ours.
 */
export function missingCodexCatalogLines(missingCatalogPath: string | null): string[] {
  if (!missingCatalogPath) return [];
  return [
    "⚠️  Codex is pointed at a model catalog that is no longer on disk, so Codex cannot load its config:",
    `   ${missingCatalogPath}`,
    "   Regenerate it with 'ocx start', or remove opencodex from Codex with 'ocx restore'.",
  ];
}

/** `mainAccountPolicy`: only `--json` asks; the human report reads the same accounts once via OAuth health. */
export async function collectStatus(options: { mainAccountPolicy?: boolean } = {}): Promise<CliStatusView> {
  const configDiagnostics = readConfigDiagnostics();
  const config = configDiagnostics.config;
  const claudeDesktop = {
    desiredEnabled: claudeDesktopIntegrationEnabled(config),
    policy: claudeDesktopPolicyHealth(probeClaudeDesktopPolicy()),
  };
  const resolvedRuntime = (() => {
    try {
      return resolveCodexRuntime();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const redacted = redactUserPath(redactSecretString(message)).slice(0, 160);
      return {
        runtime: { command: "codex", version: null, source: "fallback" as const },
        failures: [{
          command: "codex",
          source: "fallback" as const,
          reason: `resolve threw: ${redacted}`,
        }],
        replacedConfigured: undefined,
        newerAvailable: undefined,
      };
    }
  })();
  // Prefer identity-verified liveness (runtime-port + /healthz) over ocx.pid alone (#618).
  // Pass the already-resolved diagnostics config so findLiveProxy does not re-load and
  // warn on malformed config.json (status --json must stay stderr-clean).
  const live = await findLiveProxy({
    configFn: () => ({ port: config.port, hostname: config.hostname }),
  });
  const mainAccountHardLock = options.mainAccountPolicy && live
    ? (await (await import("../oauth/health")).fetchCodexHealthFromLiveProxy(undefined, async () => live)).mainAccountHardLock
    : undefined;
  // Same `--json`-only gate as the hard-lock read above: the human report stays one probe.
  const fallback = options.mainAccountPolicy
    ? await (await import("./status-fallback")).collectStatusFallback(config, {
      live: Boolean(live),
      deps: { findLiveProxy: async () => live },
    })
    : undefined;
  // Count this installation's credentials without exposing values.
  const localCredentials = options.mainAccountPolicy ? await collectLocalCredentials() : undefined;
  const pidFile = readPid();
  // Preserve an authoritative null from orphan/legacy liveness — do not restore pidFile.
  const pid = resolveStatusPid(live, pidFile);
  // No extra request: findLiveProxy's identity probe already parsed and validated the
  // healthz body, so the version came back with the liveness result.
  const versionSkew = computeVersionSkew(packageVersion(), live?.version);
  const listen = live
    ? {
      port: live.port,
      hostname: live.hostname,
      source: live.source,
      healthUrl: `http://${probeHostname(live.hostname)}:${live.port}/healthz`,
      dashboardUrl: statusDashboardUrl(live.hostname, live.port),
    }
    : selectListenTarget(config, pidFile, pidFile ? readRuntimePort(pidFile) : null);
  // findLiveProxy already identity-probed /healthz; avoid a second fetch that can race.
  const health = live
    ? {
      ok: true,
      url: listen.healthUrl,
      message: `ok (pid ${live.pid ?? "unknown"})`,
      label: `${listen.healthUrl} ok (live)`,
    }
    : await checkProxyHealth(listen);
  // Same gatherer `ocx doctor` uses, so the two commands cannot reach different verdicts
  // about the same on-disk state (review found them diverging on fallback ports).
  const staleProcessState = await probeUncleanExitState({
    live: Boolean(live),
    port: config.port,
    hostname: config.hostname,
  });
  const bunRuntime = durableBunRuntime();
  const liveStartup = live ? await fetchLiveStartupHealth(live) : null;
  const service = diagnoseService();
  // A service can be registered and still not serve: the manager reports the job
  // either way. When the identity-probed live proxy provides an attested startup verdict,
  // prefer it over a shell-local service-manager probe that lacks the service environment.
  const codexShim = diagnoseCodexShim();
  const codexShimSummary = codexShim.summary;
  const { startup, startupSource } = selectStatusStartupHealth(liveStartup, () => collectStartupHealth(config, {
    service,
    shim: codexShim,
    routingKind: getCodexRoutingKind(),
  }));
  const serviceSummary = statusServiceSummary(startup, service, Boolean(live), startupSource);
  const codexPlugins = diagnoseCodexBundledPlugins();
  const lastClamp = loadLastEffortClamp();
  const clampActive = effortClampAppliesToRuntime(lastClamp, resolvedRuntime.runtime);
  const codexHome = collectOrcaCodexHomeDiagnostic();
  const warningParts: string[] = [];
  if (
    resolvedRuntime.replacedConfigured
    && resolvedRuntime.replacedConfigured.from.command !== resolvedRuntime.runtime.command
  ) {
    warningParts.push(
      `Preferred Codex runtime is unavailable; using ${displayCodexRuntimePath(resolvedRuntime.runtime.command)} instead. Run ocx doctor for diagnosis and recovery.`,
    );
  } else if (
    resolvedRuntime.runtime.source === "fallback"
    && resolvedRuntime.failures.length > 0
    && !resolvedRuntime.runtime.version
  ) {
    const detail = resolvedRuntime.failures[0]?.reason;
    warningParts.push(
      detail
        ? `No validated Codex runtime found (${detail}); falling back to \`codex\`. Run ocx doctor for diagnosis and recovery.`
        : "No validated Codex runtime found; falling back to `codex`. Run ocx doctor for diagnosis and recovery.",
    );
  }
  if (resolvedRuntime.newerAvailable) {
    warningParts.push("OpenCodex is using an older Codex binary. Run ocx doctor for diagnosis and recovery.");
  }
  if (clampActive) {
    warningParts.push(
      `Catalog clamp removed: ${liveRemovedEfforts(lastClamp).join(", ")}. Run ocx doctor for diagnosis and recovery.`,
    );
  }
  // A Grok fence naming a port we are not listening on is invisible everywhere else:
  // grok retries the refused connection on its own side, so no request — and therefore
  // no log line — ever reaches us. Surface it here, where the live port is already known.
  const grokDrift = (() => {
    try {
      // Locally reachable is a set: the public port plus the unauthenticated loopback
      // listener's port. A fence naming either is correct; only a third port is drift (#4236).
      return grokFenceEndpointDrift(
        readGrokStatus(),
        health.ok ? listen.port : undefined,
        health.ok ? effectiveLoopbackListenerPort(config, listen.port) : null,
      );
    } catch {
      return null; // reading grok's config must never break `ocx status`
    }
  })();
  if (grokDrift) {
    warningParts.push(
      `Grok Build config points at port ${grokDrift.fencePort}, but the proxy is on `
      + `${grokDrift.livePort}; grok turns will retry against a closed port. Run 'ocx ensure' to repoint it.`,
    );
  }
  const codexDrift = (() => {
    if (!health.ok) return null;
    try {
      const siblingOfPort = readRuntimePort()?.siblingOfPort;
      return codexRoutingDriftWarning({
        content: siblingOfPort === undefined ? readFileSync(CODEX_CONFIG_PATH, "utf8") : null,
        livePort: listen.port,
        loopbackPort: effectiveLoopbackListenerPort(config, listen.port),
        siblingOfPort,
        journaled: () => ({
          openaiBaseUrl: journaledInjectedOpenaiBaseUrl({ readOnly: true }),
          realtimeWsBaseUrl: journaledInjectedRealtimeWsBaseUrl({ readOnly: true }),
        }),
      });
    } catch {
      return null; // an absent or unreadable config.toml must never break `ocx status`
    }
  })();
  if (codexDrift) warningParts.push(codexDrift);
  const codexRuntime = {
    path: displayCodexRuntimePath(resolvedRuntime.runtime.command),
    version: resolvedRuntime.runtime.version,
    source: resolvedRuntime.runtime.source,
    newerAvailable: resolvedRuntime.newerAvailable
      ? {
        path: displayCodexRuntimePath(resolvedRuntime.newerAvailable.command),
        version: resolvedRuntime.newerAvailable.version,
      }
      : null,
    warning: warningParts.length > 0 ? warningParts.join(" ") : null,
    catalogClamp: {
      active: clampActive,
      // Report what is still clamped, not what the file happens to name: a leftover written
      // before the max/ultra exemption lists rungs nothing removes any more.
      removedEfforts: clampActive ? [...liveRemovedEfforts(lastClamp)] : [],
      runtimeVersion: clampActive ? (lastClamp?.runtimeVersion ?? null) : null,
    },
  };
  const proxyLabel = live
    ? `running (PID ${live.pid ?? pid ?? "unknown"})`
    : pid && health.ok
      ? `running (PID ${pid})`
      : pid
        ? `PID file points to PID ${pid}, but health check failed`
        : health.ok
          ? "reachable, but PID file is missing or stale"
          : "not running";

  return {
    proxyLabel,
    healthLabel: health.label,
    json: {
      ...(mainAccountHardLock ? { mainAccountHardLock } : {}),
      ...(fallback ? { fallback } : {}),
      ...(localCredentials === undefined ? {} : { localCredentials }),
      schemaVersion: 1,
      appliedCodexSettingsDigest: appliedCodexSettingsDigest(config),
      proxy: {
        running: Boolean(live) || Boolean(pid && health.ok),
        pid: live?.pid ?? pid,
        staleProcessState,
        health: {
          ok: health.ok,
          url: health.url,
          message: health.message,
        },
      },
      dashboard: { url: listen.dashboardUrl },
      listen: {
        port: listen.port,
        hostname: listen.hostname ?? null,
        source: listen.source,
      },
      paths: {
        config: getConfigPath(),
        pid: getPidPath(),
        runtime: bunRuntime.path,
      },
      runtime: {
        source: bunRuntime.source,
        ...(bunRuntime.source === "override" ? { overrideEnv: bunRuntime.overrideEnv } : {}),
      },
      codexAutostart: codexAutoStartEnabled(config),
      startup,
      defaultProvider: typeof config.defaultProvider === "string" ? config.defaultProvider : null,
      config: {
        source: configDiagnostics.source,
        error: configDiagnostics.error,
      },
      service: { summary: serviceSummary },
      codexShim: { summary: codexShimSummary },
      codexPlugins,
      codexRuntime,
      codexHome,
      claudeDesktop,
      // Own field rather than a line in `codexRuntime.warning`: a stale ocx on PATH is a
      // fact about this install, not about the Codex runtime, and filing it there would
      // print it under the wrong heading (#2701).
      versionSkew,
      startupSource,
    },
  };
}
