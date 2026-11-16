/** Wire values used to render current and historical request logs. */
export const ATTEMPT_RECOVERY_KIND_ROSTER = Object.freeze([
  "transient-5xx",
  "connection-reset",
  "oauth-401",
  "oauth-account-403",
  "key-401",
  "key-429",
  "rate-limit-429",
  "anthropic-oauth-429",
  "oauth-account-429",
  "oauth-account-402",
  "image-413",
  "console-go-upload-retry",
  "opaque-blob-rejection",
  "empty-completion",
  "reasoning-effort-downgrade",
  "anthropic-fast-downgrade",
] as const);

export type AttemptRecoveryKind = (typeof ATTEMPT_RECOVERY_KIND_ROSTER)[number];

export const REQUEST_FAILURE_STAGES = Object.freeze([
  /** No response head exists. Whether the origin began the turn is not known from the stage alone. */
  "pre-header",
  /** A status line and headers exist, and no protocol body event has been parsed yet. */
  "headers-only",
  /** The protocol body began with control events only -- `response.created`, quota frames. */
  "protocol-prelude",
  /** At least one output-bearing event reached the caller. */
  "semantic-output",
  /** A tool call or other externally visible effect was emitted. */
  "side-effect",
  /** A terminal event settled the turn after its answer reached the caller. */
  "terminal",
] as const);

export type RequestFailureStage = (typeof REQUEST_FAILURE_STAGES)[number];

/**
 * Why the request failed, as one closed dictionary for every layer.
 *
 * Bounded on purpose: these are wire values a maintainer reads and a metric labels by, never a
 * credential, an account identifier, an upstream body or prompt content. That bound is what lets
 * the value be a Prometheus label and a grouping key without a masking pass -- a closed roster
 * has nothing to mask.
 */
export const REQUEST_FAILURE_CAUSES = Object.freeze([
  /** The bytes provably never reached the origin: connect refused, DNS failure, TLS handshake. */
  "transport-unsent",
  /** The bytes left and the connection died before a head. The origin may be running the turn. */
  "transport-ambiguous",
  /** The origin answered that it would not start the turn now: 503, overloaded, backpressure. */
  "upstream-declined",
  /** A 429 rate limit. Capacity is momentarily gone; waiting is the remedy. */
  "rate-limit",
  /** Plan or credit quota is gone. Waiting out a retry window does not help; the account must change. */
  "quota-exhausted",
  /** Credentials were rejected: 401, 403 on identity. */
  "credential-rejected",
  /** The origin evaluated the content and refused it. Identical bytes get the identical refusal. */
  "policy-refusal",
  /**
   * The origin rejected a request PARAMETER rather than the content: an unsupported reasoning
   * effort, an unknown field. Distinct from `policy-refusal` because the remedy is opposite --
   * the same content succeeds once the parameter is adjusted.
   */
  "parameter-rejected",
  /** Opaque replay state was rejected as unverifiable. Only a request without it can succeed. */
  "ciphertext-refusal",
  /**
   * The payload exceeded a size the origin accepts. A smaller rebuild of the same turn can
   * succeed, which is why this is not the same answer as `payload-rejected`.
   */
  "payload-too-large",
  /** The payload was rejected on its merits: unsupported media, malformed part. No repair helps. */
  "payload-rejected",
  /**
   * The origin returned a server-side fault. Whether it had already begun the turn is not
   * knowable from the status, so this is the honest classification for the mixed 5xx set the
   * transient layer retries: 503 really did decline, 500 may not have.
   */
  "upstream-fault",
  /** The turn settled carrying no usable output. */
  "empty-output",
  /** The caller went away. */
  "client-cancelled",
  /** This proxy refused before dispatch: send budget, route policy, replay refusal. */
  "local-refusal",
] as const);

export type RequestFailureCause = (typeof REQUEST_FAILURE_CAUSES)[number];

/**
 * Whether this proxy may send the request again. Derived at READ time from the stage and the
 * cause and never persisted, so a stored row cannot carry a verdict that the current table
 * would no longer reach.
 *
 * Every refusal names WHY it refused, because the three reasons need different operator
 * responses and used to arrive as one undifferentiated "no retry".
 */
export const RESEND_PERMISSIONS = Object.freeze([
  /** The same request may be sent again. */
  "permitted",
  /** Only a modified request may be sent: rotated credential, stripped ciphertext. */
  "permitted-after-repair",
  /** Upstream execution state is unknown. No AUTOMATIC resend. */
  "refused-ambiguous",
  /** The caller already observed output or an externally visible effect. */
  "refused-committed",
  /** Identical bytes would get the identical answer. */
  "refused-futile",
] as const);

export type ResendPermission = (typeof RESEND_PERMISSIONS)[number];

/**
 * What one logical request spent upstream, decomposed by how much of it is explained.
 *
 * The counting half of the durable spend record, without the routing detail that sits beside it.
 * Every surface that reports a send total reads these three numbers and none of them recomputes a
 * total of its own -- a recomputed total is how the exporter and the dashboard ended up reporting
 * different send counts for the same request.
 */
export interface RequestSpendTotals {
  /** Physical upstream sends summed across every attempt, combo children included. */
  sends: number;
  /** Sends whose attempt reached a terminal status, so the spend has a known outcome. */
  settled: number;
  /**
   * Sends charged with no terminal outcome behind them: an attempt abandoned mid-flight, or a
   * budget charge no attempt row ever accounted for. Never folded into `settled` -- an unexplained
   * send is the exact quantity this record exists to make visible.
   */
  unresolved: number;
  /** Model sends the request execution budget charged. Absent when no budget was attached. */
  reserved?: number;
}
