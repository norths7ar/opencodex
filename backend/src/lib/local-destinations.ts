import { isIP } from "node:net";
import { effectiveLoopbackListenerPort, isLoopbackHostname, isWildcardHostname, shouldInjectApiAuthHeader } from "../codex/loopback-target";
import { probeHostname } from "../server/proxy-liveness";
import { resolveDataPlaneAdmissionSecret } from "../server/auth-cors";
import { loadServiceTokenFromFile, serviceApiTokenFilePath } from "./service-secrets";
import type { OcxConfig } from "../types";

export type LocalInferenceConfig = Pick<OcxConfig, "hostname" | "unauthenticatedLoopbackListener">;
export type LocalManagementConfig = Pick<OcxConfig, "hostname">;

export interface LocalInferenceDestination {
  /** Origin a local client wire dials, e.g. `http://127.0.0.1:10104`. */
  origin: string;
  /** Port component of `origin`. */
  port: number;
  /**
   * Does the listener at `origin` demand `x-opencodex-api-key`?
   *
   * False only for the unauthenticated loopback listener and a genuinely loopback public bind.
   * A caller that cannot attach a credential must log that it is degrading rather than write a
   * destination that answers 401.
   */
  requiresAdmissionToken: boolean;
}

/**
 * Turn a bind setting into an address that is safe to combine with local credentials.
 *
 * Bun resolves a DNS bind name when the listener starts, but a later client lookup can receive
 * a different answer. Local integrations therefore use only literal addresses; a DNS bind falls
 * back to loopback and fails closed when the listener does not answer there. Operators who need
 * local integrations on a DNS-bound listener can enable the dedicated loopback listener.
 *
 * `localhost` is deliberately NOT treated as a DNS bind here, even though it is a name. It is
 * reserved to loopback by RFC 6761, so a second lookup cannot select a peer off this machine —
 * the thing this function exists to prevent. Rewriting it to `127.0.0.1` would buy no safety and
 * would change what every existing loopback install writes into its exported client
 * configuration, which is a contract `tests/claude-integration/claude-cli.test.ts` pins.
 * `isLoopbackHostname` is the existing encoding of "this name is loopback", so the two stay in
 * agreement by construction rather than by a second list.
 */
export function localCredentialDestinationHostname(hostname: string | undefined): string {
  const probed = probeHostname(hostname);
  if (isLoopbackHostname(probed)) return probed;
  const literal = probed.startsWith("[") && probed.endsWith("]")
    ? probed.slice(1, -1)
    : probed;
  return isIP(literal) !== 0 ? probed : "127.0.0.1";
}

/**
 * The one answer for "where does a local client send inference, and does it need a key?".
 *
 * Kept in one place so the day the resolution changes a forgotten site cannot point a client
 * config at a closed socket — which is precisely the defect this module exists to close.
 */
export function localInferenceDestination(
  config: LocalInferenceConfig | undefined,
  publicPort: number,
): LocalInferenceDestination {
  const listenerPort = effectiveLoopbackListenerPort(config, publicPort);
  if (listenerPort !== null) {
    // Always bound to 127.0.0.1 and admits local callers with no credential, which is what
    // lets an exported client configuration stay credential-free.
    return { origin: `http://127.0.0.1:${listenerPort}`, port: listenerPort, requiresAdmissionToken: false };
  }
  const hostname = config?.hostname;
  // A loopback bind keeps the byte-identical string every call site wrote before this module
  // existed, including the `localhost`/`::1` spellings that `probeHostname` would preserve.
  if (isLoopbackHostname(hostname)) {
    return { origin: `http://127.0.0.1:${publicPort}`, port: publicPort, requiresAdmissionToken: false };
  }
  // `probeHostname` turns every all-zero spelling into 127.0.0.1 and brackets a bare IPv6
  // literal; `shouldInjectApiAuthHeader` is the existing encoding of "this bind demands a
  // data-plane credential", so the two stay in agreement by construction.
  return {
    origin: `http://${localCredentialDestinationHostname(hostname)}:${publicPort}`,
    port: publicPort,
    requiresAdmissionToken: shouldInjectApiAuthHeader(config),
  };
}

/**
 * Every port this proxy's data plane answers on at 127.0.0.1 — the set an inherited
 * `http://127.0.0.1:<port>` base URL must hit to count as one of OURS.
 *
 * On a tailnet bind with no loopback listener the set is EMPTY, and that is the point: a
 * leftover `http://127.0.0.1:10100` from a previous loopback-bound install is a dead socket
 * there, so `ocx claude` must replace it rather than preserve it as its own destination.
 */
export function localLoopbackInferencePorts(
  config: LocalInferenceConfig | undefined,
  publicPort: number,
): number[] {
  const ports: number[] = [];
  // A wildcard bind owns loopback on the public port too, so a URL naming it is still ours.
  if (isLoopbackHostname(config?.hostname) || isWildcardHostname(config?.hostname)) ports.push(publicPort);
  const listenerPort = effectiveLoopbackListenerPort(config, publicPort);
  if (listenerPort !== null && !ports.includes(listenerPort)) ports.push(listenerPort);
  return ports;
}

/**
 * The DATA-PLANE admission credential this host can present to its own public listener.
 *
 * Same ladder the Codex provider table and `ocx opencode` already use — environment token,
 * hardened service token file, first configured `apiKeys` entry — and deliberately NOT the
 * admin token, which must never leave the management surface. `undefined` means the caller has
 * nothing to attach and has to degrade loudly.
 */
export function localAdmissionToken(
  config: Pick<OcxConfig, "apiKeys"> | undefined,
  env: NodeJS.ProcessEnv = process.env,
): string | undefined {
  return localAdmissionTokenCandidates(config, env)[0];
}

function localAdmissionTokenCandidates(
  config: Pick<OcxConfig, "apiKeys"> | undefined,
  env: NodeJS.ProcessEnv,
): string[] {
  const candidates: string[] = [];
  const envToken = env.OPENCODEX_API_AUTH_TOKEN?.trim();
  if (envToken) candidates.push(envToken);
  const lookup = env.OCX_API_TOKEN_FILE?.trim()
    ? env
    : { ...env, OCX_API_TOKEN_FILE: serviceApiTokenFilePath() };
  const fileToken = loadServiceTokenFromFile(lookup as Record<string, string | undefined>)?.trim();
  // Shape-check the FILE candidate only. The env var and `apiKeys` are values an operator set
  // deliberately and pass through verbatim; a path, by contrast, can be pointed at or replaced
  // by something that is not a credential at all, and sending that as one leaks file contents
  // into a request header. Configured keys are never checked, so no existing key can be broken.
  if (fileToken && ADMISSION_TOKEN_SHAPE.test(fileToken)) candidates.push(fileToken);
  const configured = config?.apiKeys?.find(entry => entry.key.trim().length > 0)?.key.trim();
  if (configured) candidates.push(configured);
  return candidates;
}

/**
 * The credential an in-process self-fetch (routed helper) presents to `destination`.
 *
 * A destination that demands admission gets the `localAdmissionToken` ladder unchanged. One that
 * does not still receives the first ladder credential this data plane recognizes, because Chat
 * applies that key's limits to an internal hop a loopback listener admitted without reading it
 * (#7894). An unrecognized one, such as a hub client key a loopback bind deliberately left out of
 * its admission credentials, is not sent: Chat refuses any credential it cannot resolve.
 */
export function selfFetchAdmissionToken(
  config: Pick<OcxConfig, "apiKeys"> | undefined,
  destination: Pick<LocalInferenceDestination, "requiresAdmissionToken">,
  env: NodeJS.ProcessEnv = process.env,
): string | undefined {
  const candidates = localAdmissionTokenCandidates(config, env);
  if (destination.requiresAdmissionToken) return candidates[0];
  return candidates.find(token => resolveDataPlaneAdmissionSecret(token, config ?? {}) !== null);
}

/** An admission credential is an opaque printable token — never JSON, a path, or multi-line. */
const ADMISSION_TOKEN_SHAPE = /^[A-Za-z0-9._~+/=-]{8,4096}$/;

/**
 * The origin a local CLI dials for `/api/*`.
 *
 * The caller still supplies the management credential. Never write that credential into an
 * exported client configuration.
 */
export function localManagementOrigin(
  config: LocalManagementConfig | undefined,
  publicPort: number,
): string {
  return `http://${localCredentialDestinationHostname(config?.hostname)}:${publicPort}`;
}
