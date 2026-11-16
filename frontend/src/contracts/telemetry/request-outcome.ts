export const REQUEST_OUTCOME_CLASSES = Object.freeze([
  /** A terminal event settled the turn and the caller received the answer. */
  "completed",
  /** The turn ended without an answer. */
  "failed",
  /** The turn produced part of an answer and stopped. */
  "incomplete",
  /** The caller went away before the turn finished. */
  "aborted",
] as const);

export type RequestOutcomeClass = (typeof REQUEST_OUTCOME_CLASSES)[number];

/**
 * The facts a terminal classification is allowed to read.
 *
 * Deliberately narrow, and deliberately NOT the whole durable row: an outcome that could consult
 * a provider name or an error message would be a different answer per provider, which is how the
 * three surfaces drifted apart in the first place.
 */
export interface RequestOutcomeFacts {
  readonly status: number;
  readonly terminalStatus?: string | undefined;
  readonly closeReason?:
    | "terminal"
    | "client_cancel"
    | "non_stream"
    | "body_stall"
    | "body_overflow"
    | "body_interrupted"
    | undefined;
}

/**
 * Classify one finished logical request.
 *
 * Semantic terminal facts are read BEFORE the numeric status, which is the whole point. An HTTP
 * 200 that carried an incomplete terminal is incomplete; a 502 that carried an incomplete
 * terminal is also incomplete, and reading the status first would have called them success and
 * failure. The numeric status is consulted only when no terminal event was recorded at all.
 */
export function classifyRequestOutcome(facts: RequestOutcomeFacts): RequestOutcomeClass {
  if (facts.closeReason === "client_cancel" || facts.status === 499) return "aborted";
  if (facts.terminalStatus === "failed") return "failed";
  if (
    facts.terminalStatus === "incomplete" ||
    facts.closeReason === "body_stall" ||
    facts.closeReason === "body_overflow" ||
    facts.closeReason === "body_interrupted"
  )
    return "incomplete";
  if (facts.terminalStatus === "completed") return "completed";
  if (
    facts.terminalStatus === undefined &&
    (facts.status === 101 || (facts.status >= 200 && facts.status < 400))
  )
    return "completed";
  return "failed";
}
