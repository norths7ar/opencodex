export const PROTOCOLS = ["responses", "chat", "messages"] as const;
/** A public inference API a client can speak to this proxy. */
export type Protocol = (typeof PROTOCOLS)[number];

export const UPSTREAM_WIRES = ["responses", "chat", "messages", "other"] as const;
/**
 * The wire the final provider receives. `other` covers adapters whose upstream is none of
 * the three public protocols (Gemini, Kiro, Cursor, ...); nothing is claimed about them here.
 */
export type UpstreamWire = (typeof UPSTREAM_WIRES)[number];

export const PROTOCOL_HOPS = [
  "responses",
  "chat",
  "messages",
  "other",
  "ir",
  "responses-internal",
] as const;
/**
 * One node of a request or response path.
 *
 * - a wire name: that wire's JSON/SSE is actually produced at this point;
 * - `ir`: the adapter-neutral request (`OcxParsedRequest`) or event (`AdapterEvent`) form;
 * - `responses-internal`: Responses JSON/SSE produced only as an internal bridge, never sent to
 *   the provider. Its presence is what makes a path `legacy-bridge`.
 */
export type ProtocolHop = (typeof PROTOCOL_HOPS)[number];

export const DELIVERY_MODES = ["native", "translated", "legacy-bridge", "blocked"] as const;
/**
 * How one request (or one attempt) reached its upstream.
 *
 * - `native`: same wire end to end; the source body is the wire source. Model rewrites, auth
 *   injection and declared provider policy can still apply, so native is not byte-identical.
 * - `translated`: cross-wire conversion whose only intermediate is the IR or the target wire.
 * - `legacy-bridge`: the path contains `responses-internal`.
 * - `blocked`: refused before any upstream send.
 */
export type DeliveryMode = (typeof DELIVERY_MODES)[number];

export const FIDELITIES = ["preserved", "degraded", "unknown"] as const;
export type Fidelity = (typeof FIDELITIES)[number];

/**
 * Fixed reason codes. Never conversation-derived, never free text: plan and trace records carry
 * only these, which is what keeps them safe to persist and to show on a remote dashboard.
 */
export const PROTOCOL_REASON_CODES = [
  "same-wire-native",
  "cross-wire-codec",
  "cross-wire-ir",
  "policy-route",
  "responses-only-feature",
  "hosted-tool",
  "vision-preprocessing",
  "tool-result-image",
  "auth-mode-not-native",
  "effort-row",
  "fast-row",
  "caller-credential-required",
  "not-migrated",
  "surface-disabled",
  "feature-unrepresentable",
  "compatibility-reject",
  "unknown-model",
  "upstream-other",
  "rollout-disabled",
  /** Operator policy that only the bridge applies (pinned effort, skill elision, a sidecar). */
  "bridge-only-policy",
  /** A pooled Anthropic OAuth account set: the bridge owns rotation and affinity. */
  "oauth-account-pool",
  /** Caller `anthropic-beta` values outside the native lane's allowlist were not forwarded. */
  "anthropic-beta-dropped",
  /** Thinking signatures or `redacted_thinking` removed for a destination that cannot verify them. */
  "opaque-state-stripped",
] as const;
export type ProtocolReasonCode = (typeof PROTOCOL_REASON_CODES)[number];

const PROTOCOL_SET = new Set<string>(PROTOCOLS);
const UPSTREAM_SET = new Set<string>(UPSTREAM_WIRES);
const HOP_SET = new Set<string>(PROTOCOL_HOPS);
const MODE_SET = new Set<string>(DELIVERY_MODES);
const FIDELITY_SET = new Set<string>(FIDELITIES);
const REASON_SET = new Set<string>(PROTOCOL_REASON_CODES);

export function isProtocol(value: unknown): value is Protocol {
  return typeof value === "string" && PROTOCOL_SET.has(value);
}
export function isUpstreamWire(value: unknown): value is UpstreamWire {
  return typeof value === "string" && UPSTREAM_SET.has(value);
}
export function isProtocolHop(value: unknown): value is ProtocolHop {
  return typeof value === "string" && HOP_SET.has(value);
}
export function isDeliveryMode(value: unknown): value is DeliveryMode {
  return typeof value === "string" && MODE_SET.has(value);
}
export function isFidelity(value: unknown): value is Fidelity {
  return typeof value === "string" && FIDELITY_SET.has(value);
}
export function isProtocolReasonCode(value: unknown): value is ProtocolReasonCode {
  return typeof value === "string" && REASON_SET.has(value);
}

/**
 * The upstream wire a provider adapter id speaks. Only the three adapters whose request body
 * is one of the public protocols map to a protocol; every other adapter is `other`.
 */
export function upstreamWireForAdapter(adapter: string): UpstreamWire {
  switch (adapter) {
    case "openai-responses":
      return "responses";
    case "openai-chat":
      return "chat";
    case "anthropic":
      return "messages";
    default:
      return "other";
  }
}
