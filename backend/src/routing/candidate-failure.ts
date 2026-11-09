import { classifyError, isCyberPolicyCode } from "../lib/errors";
import { isNonReplayableUpstreamCode } from "../lib/upstream-retry";
import { isAnthropicBillingFailure } from "../providers/anthropic-billing-refusal";
import { isKiroAccountProfileRefusal } from "../adapters/kiro-refusal";
import { isDefiniteContextOverflow } from "../lib/context-overflow-verdict";

export type CandidateFailureDecision = "hop" | "stop";

function normalizedFailureCode(code?: string | null): string {
  return code?.trim().toLowerCase().replaceAll("-", "_") ?? "";
}

/**
 * A spent account window, by structured code or upstream prose. Status is deliberately not
 * consulted: the ChatGPT Codex backend reports a depleted plan window as HTTP 502
 * `upstream_server_error` carrying `The usage limit has been reached`, never the documented 429,
 * so any status gate misses it. Read in exactly ONE place -- the cooldown DURATION fallback. A
 * status-blind prose match is safe for choosing how long to wait; it is not safe for choosing
 * what to black out, so `isProviderScopedQuotaCap` and the scope/decision paths stay untouched
 * and a Codex 502 still resolves `target` scope and `hop` through `status >= 500`.
 */
// `1308` is the vendor code for a spent five-hour window and carries no prose of its own when the
// upstream reports it bare, so it belongs here too. The rest of QUOTA_LIMIT_CODES stays out: those
// are quota-limit codes whose window length this gateway has no evidence for, and guessing long on
// them would hold a target that may clear sooner.
const ACCOUNT_EXHAUSTION_CODES = new Set(["usage_limit_exceeded", "usage_limit_reached", "1308"]);
// Token-plan windows (Alibaba's DeepSeek/Qwen plans) report "Your token-plan 1-week quota has been
// exhausted" (#5494). The match is anchored to that phrasing: a looser "quota ... exhausted" would
// also catch per-minute limits, and this arm outranks the transient rate-limit duration.
const ACCOUNT_EXHAUSTION_TEXT = /usage limit (?:has been )?reached|token-plan\s+\S+\s+quota has been exhausted|(?:account|weekly|monthly|daily)\s*(?:usage\s+)?quota (?:(?:has been |is )?exhausted|exceeded)/i;

/**
 * Determines whether an error signals that an account's quota or usage window has been exhausted.
 *
 * Explicit error codes (`usage_limit_exceeded`, `insufficient_quota`, etc.) are always authoritative.
 * For HTTP 400 (Bad Request), prose alone is not authoritative because client validation errors
 * may echo prompt contents or parameter values containing quota-related words.
 *
 * @param message The error message text.
 * @param code The normalized or raw error code.
 * @param status The HTTP status code, if known.
 * @returns True if the failure represents an account window exhaustion.
 */
function isAccountWindowExhausted(
  message: string,
  code?: string | null,
  status?: number,
): boolean {
  const normalized = normalizedFailureCode(code);
  if (ACCOUNT_EXHAUSTION_CODES.has(normalized) || normalized === "insufficient_quota") {
    return true;
  }
  // On HTTP 400 (Bad Request / parameter validation), prose alone is not authoritative evidence
  // of account quota exhaustion because validation errors may echo user prompts or parameter values
  // quoting "quota exhausted". An authoritative account exhaustion verdict on 400 requires a
  // structured exhaustion code.
  if (status === 400 || status === 422) {
    return false;
  }
  return ACCOUNT_EXHAUSTION_TEXT.test(message.toLowerCase());
}

function isProviderScopedQuotaCap(
  status: number | undefined,
  message: string,
  code?: string | null,
): boolean {
  if (status === 400 || status === 422) return false; // Validation prose may reflect the caller's input.
  const normalizedCode = normalizedFailureCode(code);
  const text = message.toLowerCase();
  if (
    status === 429
    && (normalizedCode === "gousagelimiterror" || text.includes("monthly usage limit reached"))
  ) {
    return true;
  }
  return text.includes("err_free_prompt_cap")
    || (text.includes("free tier") && text.includes("single request"));
}

/** A structured account-window exhaustion code; prose never qualifies (a request can echo it). */
function hasAccountExhaustionCode(code?: string | null): boolean {
  const normalized = normalizedFailureCode(code);
  return ACCOUNT_EXHAUSTION_CODES.has(normalized) || normalized === "insufficient_quota";
}

/**
 * A free-tier cap the upstream evaluates PER REQUEST rather than per account window. These
 * needles used to reach only `isProviderScopedQuotaCap`, so a single oversized free-tier prompt
 * cooled the whole provider for every other combo — including the shorter requests that same
 * provider would still have served. `free_rate_limited` also left the provider-scoped predicate
 * for the same reason; it stays a hop signal, but stops recording provider-wide evidence.
 */
function isRequestLocalFreePromptCap(
  status: number | undefined,
  message: string,
  code?: string | null,
): boolean {
  if (status !== 400 && status !== 422) return false;
  const text = message.toLowerCase();
  if (normalizedFailureCode(code) === "free_rate_limited") return true;
  if (text.includes("err_free_prompt_cap")) return true;
  return text.includes("free tier") && (text.includes("single request") || text.includes("prompt"));
}

/**
 * Precise target-local request incompatibilities are request-local, not terminal for a combo.
 * Require a bounded, intact provider envelope; never infer compatibility from echoed prompt text.
 * Only OpenCodex's exact error wrapper may be unwrapped, with a fixed depth budget. Unknown or
 * conflicting codes fail closed. No fields are removed here and no same-target replay is added.
 * Image rejection requires `param: input` and an exact model-scoped prefix.
 */
function isRequestLocalTargetIncompatibility(status: number, message: string, code?: string | null): boolean {
  if (status !== 400 || message.length > 16_384) return false;
  const genericCodes = new Set(["", "invalid_request_error", "unsupported_parameter", "unsupported_value"]);
  if (!genericCodes.has(normalizedFailureCode(code))) return false;
  let text = message.trim();
  for (let depth = 0; depth < 3; depth += 1) {
    if (text.startsWith("Provider error 400: ")) text = text.slice("Provider error 400: ".length);
    let payload: unknown;
    try { payload = JSON.parse(text); } catch { return false; }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
    const error = (payload as Record<string, unknown>).error;
    if (!error || typeof error !== "object" || Array.isArray(error)) return false;
    const e = error as Record<string, unknown>;
    if (e.code !== undefined && e.code !== null && typeof e.code !== "string") return false;
    const errorCode = normalizedFailureCode(typeof e.code === "string" ? e.code : undefined);
    if (!genericCodes.has(errorCode) || typeof e.message !== "string") return false;
    if (e.type !== "invalid_request_error" && e.type !== "upstream_error") return false;
    if (e.message.startsWith("Provider error 400: ") && e.param === undefined
      && (errorCode === "" || errorCode === "invalid_request_error")) {
      text = e.message;
      continue;
    }
    if (e.type !== "invalid_request_error") return false;
    if (e.message === "Unsupported parameter: user") {
      return (e.param === undefined || e.param === "user") && errorCode !== "unsupported_value";
    }
    if (errorCode === "unsupported_value"
      && (e.param === "reasoning.effort" || e.param === "reasoning_effort")
      && e.message.startsWith("Unsupported value:") && e.message.includes("not supported")) return true;
    if (e.code === null
      && e.param === "reasoning_effort"
      && /^Function tools with reasoning_effort are not supported for gpt-6-astra(?:-\d{4}-\d{2}-\d{2})? in \/v1\/chat\/completions\. To use function tools, use \/v1\/responses or set reasoning_effort to 'none'\.$/.test(e.message)) return true;
    return e.param === "input"
      && (errorCode === "" || errorCode === "invalid_request_error")
      && /^Model '[^']{1,256}' does not support image inputs\./.test(e.message);
  }
  return false;
}

/**
 * Codes a gateway uses when it declines a request field it cannot serve.
 *
 * Wider than the set {@link isRequestLocalTargetIncompatibility} accepts, by exactly one member:
 * Alibaba's gateway reports `invalid_parameter_error`. It is kept in its own set rather than
 * added to the shared one, because that set also governs the `user` parameter and image-input
 * branches and widening it there would admit shapes those branches were reasoned about without.
 */
const RESPONSE_FORMAT_REFUSAL_CODES = new Set([
  "",
  "invalid_request_error",
  "invalid_parameter_error",
  "unsupported_parameter",
  "unsupported_value",
]);

/**
 * Does this message say the target cannot PROVIDE `response_format`, rather than that the
 * request's `response_format` was malformed?
 *
 * That distinction is the whole point of #4903 and it is why neither obvious option was taken.
 * Hopping on every 400 would replay a genuinely malformed request against every remaining
 * target. Dropping `response_format` would silently change the output contract the caller
 * asked for, on a path whose entire purpose is a structured result.
 *
 * So both halves are required: the message must name the field, AND it must say the field is
 * unavailable or unsupported. "Invalid schema for response_format" names the field and claims
 * nothing about capability, so it stays terminal.
 *
 * `param` may be absent or explicitly null -- the reported gateway sends `param: null` -- but a
 * param naming a DIFFERENT field contradicts the message and fails closed.
 */
function namesResponseFormatIncapability(message: string, param: unknown): boolean {
  if (param !== undefined && param !== null && param !== "response_format") return false;
  const text = message.toLowerCase();
  if (!text.includes("response_format")) return false;
  return /(unavailable|not available|unsupported|not supported|does not support|doesn't support|cannot be used|is not enabled)/u
    .test(text);
}

/**
 * A `response_format` capability gap is target-local: this model cannot produce the requested
 * output shape, which says nothing about the next target in the combo.
 *
 * Reported against a shadow title-generation call, where a combo's first target rejects
 * `response_format` and the chain stops instead of trying the target behind it (#4903).
 *
 * The next target receives the SAME request, `response_format` included, so a target that can
 * honour the contract honours it and one that cannot is skipped in turn. Traversal stays finite
 * because combo excludes each attempted target and policy tries each candidate once.
 *
 * The envelope is bounded exactly like {@link isRequestLocalTargetIncompatibility}: an intact
 * provider JSON object, a depth budget, `type: "invalid_request_error"`, and a code from a
 * closed set. Nothing is inferred from echoed prompt text and no field is removed.
 */
function isResponseFormatCapabilityRefusal(
  status: number,
  message: string,
  code?: string | null,
): boolean {
  if (status !== 400 || message.length > 16_384) return false;
  if (!RESPONSE_FORMAT_REFUSAL_CODES.has(normalizedFailureCode(code))) return false;
  let text = message.trim();
  for (let depth = 0; depth < 3; depth += 1) {
    if (text.startsWith("Provider error 400: ")) {
      text = text.slice("Provider error 400: ".length).trim();
    }
    // A chat gateway can report the refusal inside a single SSE frame, and the combo consumer
    // keeps the raw text when that frame stops the error object from being extracted -- which is
    // why the reported classification text reads `data: {"error":...}` and why the structured
    // code arrives undefined. Exactly one `data:` prefix is removed, and only when the body is
    // one line: this unwraps a single frame rather than parsing a stream, so a multi-event body
    // is left alone and still fails closed.
    if (text.startsWith("data:") && !text.includes("\n")) {
      text = text.slice("data:".length).trim();
    }
    let payload: unknown;
    try { payload = JSON.parse(text); } catch { return false; }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
    const error = (payload as Record<string, unknown>).error;
    if (!error || typeof error !== "object" || Array.isArray(error)) return false;
    const e = error as Record<string, unknown>;
    if (e.code !== undefined && e.code !== null && typeof e.code !== "string") return false;
    if (typeof e.message !== "string") return false;
    // Our own wrapper, re-wrapped by a downstream hop. Peel it and look again, within budget.
    if (e.message.startsWith("Provider error 400: ") && e.param === undefined) {
      text = e.message;
      continue;
    }
    if (e.type !== "invalid_request_error") return false;
    const errorCode = normalizedFailureCode(typeof e.code === "string" ? e.code : undefined);
    if (!RESPONSE_FORMAT_REFUSAL_CODES.has(errorCode)) return false;
    return namesResponseFormatIncapability(e.message, e.param);
  }
  return false;
}

function isModelLifecycleGone(
  status: number,
  message: string,
  code?: string | null,
): boolean {
  if (status !== 410) return false;
  const normalizedCode = code?.trim().toLowerCase().replaceAll("-", "_");
  if ([
    "model_deprecated",
    "model_end_of_life",
    "model_eol",
    "model_not_found",
    "model_retired",
  ].includes(normalizedCode ?? "")) return true;
  const text = message.toLowerCase();
  return /\bmodel\b/.test(text) && (
    /\bend[ -]of[ -]life\b/.test(text)
    || /\bno longer available\b/.test(text)
    || /\b(?:deprecated|retired|retirement|sunset|decommissioned)\b/.test(text)
  );
}

function isProviderTargetContextOverflow(
  status: number,
  message: string,
  code?: string | null,
): boolean {
  if (status !== 400) return false;
  const normalizedCode = normalizedFailureCode(code);
  const text = message.toLowerCase();
  if (text.includes("invalid_request_prompt_too_long")) return true;
  return normalizedCode === "5059"
    && /\bprompt\s+\d+\s*>\s*\d+\s+maximum context length\b/i.test(message);
}

const CODEX_ACCOUNT_MODEL_REFUSAL = /^The '[^']{1,256}' model is not supported when using Codex with a ChatGPT account\.$/;

export type CodexAccountModelRefusal = "other" | "refusal" | "ambiguous";

/** Inspect only the original root and its own response record, before carrier selection. */
export function hasConflictingCodexModelRefusalEnvelopes(status: number, payload: unknown): boolean {
  if (status !== 400 || !payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const response = Object.hasOwn(payload, "response") ? (payload as Record<string, unknown>).response : undefined;
  const nested = response && typeof response === "object" && !Array.isArray(response) ? response : undefined;
  return (Object.hasOwn(payload, "detail") || !!nested && Object.hasOwn(nested, "detail"))
    && (Object.hasOwn(payload, "error") || !!nested && Object.hasOwn(nested, "error"));
}

/** Inspect an already parsed, bounded error frame without copying or walking its body. */
export function codexAccountModelRefusalPayload(status: number, payload: unknown): CodexAccountModelRefusal {
  if (status !== 400 || !payload || typeof payload !== "object" || Array.isArray(payload)) return "other";
  if (Object.hasOwn(payload, "detail") && Object.hasOwn(payload, "error")) return "ambiguous";
  const { detail, error } = payload as { detail?: unknown; error?: unknown };
  const field = typeof detail === "string" ? detail
    : error && typeof error === "object" && !Array.isArray(error)
      && typeof (error as Record<string, unknown>).message === "string"
      ? (error as { message: string }).message : undefined;
  return field !== undefined && CODEX_ACCOUNT_MODEL_REFUSAL.test(field) ? "refusal" : "other";
}

/** One complete, bounded HTTP envelope; accept only a single exact status prefix. */
function parseCodexRefusalEnvelope(status: number, message: string): { text: string; payload: unknown } | undefined {
  if (status !== 400 || message.length > 16_384) return undefined;
  let text = message.trim();
  if (text.startsWith("Provider error 400: ")) text = text.slice("Provider error 400: ".length);
  let payload: unknown;
  try { payload = JSON.parse(text); } catch { /* Bare refusal text is also a supported carrier. */ }
  return { text, payload };
}

/** Only allowlisted hard stops cross the same HTTP boundary as positive refusal evidence. */
export function codexAccountModelRefusalHardStopCode(status: number, message: string): string | undefined {
  const payload = parseCodexRefusalEnvelope(status, message)?.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
  const root = payload as Record<string, unknown>;
  const response = Object.hasOwn(root, "response") ? root.response : undefined;
  const records = response && typeof response === "object" && !Array.isArray(response)
    ? [root, response as Record<string, unknown>] : [root];
  for (const record of records) {
    const error = record.error && typeof record.error === "object" && !Array.isArray(record.error)
      ? record.error as Record<string, unknown> : undefined;
    // Diagnostic type is never code evidence, even when a code is absent or malformed.
    for (const code of [error?.code, record.code]) {
      if (typeof code !== "string") continue;
      if (isNonReplayableUpstreamCode(code) || isCyberPolicyCode(code)) return code;
      if (normalizedFailureCode(code) === "origin_rejected") return "origin_rejected";
    }
  }
  return undefined;
}

/** A bounded fallback signal, never credential or account-entitlement evidence. */
export function codexAccountModelRefusal(
  status: number, message: string, options?: { allowNestedResponse?: boolean },
): CodexAccountModelRefusal {
  const envelope = parseCodexRefusalEnvelope(status, message);
  if (!envelope) return "other";
  if (CODEX_ACCOUNT_MODEL_REFUSAL.test(envelope.text)) return "refusal";
  const payload = envelope.payload;
  if (hasConflictingCodexModelRefusalEnvelopes(status, payload)) return "ambiguous";
  // A present root carrier stays authoritative, even when malformed or nonmatching.
  if (options?.allowNestedResponse !== false && payload && typeof payload === "object" && !Array.isArray(payload)
    && !Object.hasOwn(payload, "detail") && !Object.hasOwn(payload, "error")
    && Object.hasOwn(payload, "response")) {
    return codexAccountModelRefusalPayload(status, (payload as Record<string, unknown>).response);
  }
  return codexAccountModelRefusalPayload(status, payload);
}

export function candidateFailureDecision(
  status: number,
  message: string,
  options?: {
    code?: string | null;
    codexModelRefusal?: CodexAccountModelRefusal;
    failedAdapter?: string;
    /** The caller proved the failure is an adapter-normalized plan refusal (`isModelNotInPlanRefusal`). */
    verifiedPlanRefusal?: boolean;
  },
): CandidateFailureDecision {
  if (status === 499) return "stop";
  if (message.toLowerCase().includes("origin_rejected")) return "stop";
  // Structured form of the same hard refusal. The prose test above misses it when the origin
  // reports the code out of band, and every hop rule below -- including the context-overflow
  // one -- must stay subordinate to it.
  if (normalizedFailureCode(options?.code) === "origin_rejected") return "stop";
  // The origin may already be executing this turn (the Codex WebSocket relay sent the create
  // frame and never saw a response event). Hopping would send the same request to a second
  // target while the first may still be generating; the honest status goes to the client.
  if (isNonReplayableUpstreamCode(options?.code)) return "stop";
  // Cyber policy is a hard non-retryable refusal — honor structured code even when
  // classificationText was truncated before the JSON code field.
  if (isCyberPolicyCode(options?.code)) return "stop";
  // HTTP consumers retain hard codes before truncation; SSE metadata owns its selected carrier.
  if (options?.codexModelRefusal === undefined && codexAccountModelRefusalHardStopCode(status, message)) return "stop";
  const modelRefusal = status === 400
    ? options?.codexModelRefusal ?? codexAccountModelRefusal(status, message) : "other";
  // Competing envelopes cannot grant a hop through an earlier structured-code rule either.
  if (modelRefusal === "ambiguous") return "stop";
  // HTTP 410 is normally terminal. A model-specific lifecycle verdict is target-local,
  // however: another provider/model in the configured profile can still serve the request.
  // Require structured lifecycle code or explicit model+lifecycle prose so unrelated
  // application-level 410 responses remain fail-closed.
  if (isModelLifecycleGone(status, message, options?.code)) return "hop";
  const error = classifyError(status, "upstream_error", message);
  if (isCyberPolicyCode(error.code)) return "stop";
  // A provider can expose its own target hard cap with a non-semantic vendor code
  // (for example 5059 + invalid_request_prompt_too_long). That is evidence that this
  // target is too small, not that every later candidate is incapable of serving it.
  if (isProviderTargetContextOverflow(status, message, options?.code)) return "hop";
  // A definite context-window refusal is target-local inside a multi-candidate route: this model
  // cannot hold the turn, but a later target may have a larger window. Two boundaries keep this
  // safe. It is reached only after cancellation, structured origin/cyber refusals and
  // non-replayable post-send codes have already stopped. And it only ever classifies a failure
  // the stream preflight already proved emitted no output: `streamPayloadCommitsOutput`
  // commits the child on any text, tool call or unknown event, and only a zero-output terminal
  // becomes a failure response at all, so a turn whose text the client already saw is never
  // reclassified here.
  if (isDefiniteContextOverflow(status, message)) return "hop";
  // A local input-admission refusal (#1524) says "this candidate cannot fit the request",
  // not "the request is impossible": the next candidate may have a larger context window.
  //
  // This MUST be tested before the generic stop list below. Our own refusal message says
  // "context window" -- that is what it refuses on -- and the classifier remaps that phrase,
  // so checking the stop list first swallowed the signal and ended the chain. Confirmed
  // upstream context refusals already took the bounded message-classification hop above.
  //
  // Matched on the STRUCTURED code only, which classifyError now preserves for our own
  // refusal. A raw substring test would additionally let any upstream override a terminal
  // verdict by echoing the token in prose we do not control.
  //
  // Precise about what this is NOT: an upstream can still SET this code deliberately, since
  // both extractors read the upstream error object. That is bounded rather than dangerous --
  // an upstream already controls other hop signals (429, 5xx), and traversal is finite: policy
  // tries each candidate once via `tried`. So this is
  // structured-code-only, not provably local.
  if (options?.code === "input_admission_refused" || error.code === "input_admission_refused") {
    return "hop";
  }
  if (isProviderScopedQuotaCap(status, message, options?.code || error.code)) {
    return "hop";
  }
  // A model-scoped rejection is target-local: this provider does not serve THIS model, which
  // says nothing about the next candidate. Structured code only, plus the explicit prose
  // form upstreams emit when they carry no code, so an unrelated 400 stays terminal.
  const failureCode = normalizedFailureCode(options?.code || error.code);
  // The input-modality refusal is the same kind of verdict: this route cannot read the turn's
  // media, and a later target may. Its 400 would otherwise end the chain as invalid_request_error.
  if (["model_not_found", "model_unavailable", "unsupported_model", "unsupported_input_modality"].includes(failureCode)) {
    return "hop";
  }
  // So is a plan refusal (#7684): this account's plan excludes THIS model, and it cools only the
  // refused target because `comboFailureCooldownScope` reads a 400 as target-scoped. Unlike the
  // codes above it is honoured only with caller-supplied provenance, never from the code alone.
  if (failureCode === "model_not_in_plan" && options?.verifiedPlanRefusal === true) return "hop";
  if (modelRefusal === "refusal") return "hop";
  // `free_rate_limited` no longer routes through `isProviderScopedQuotaCap` (it is a
  // per-request cap, not provider-wide evidence), so keep its hop verdict explicit here.
  if (failureCode === "free_rate_limited") return "hop";
  if (isRequestLocalTargetIncompatibility(status, message, options?.code)) return "hop";
  // Must precede the generic `invalid_request_error` stop below, which is where this refusal
  // ended the chain: the gateway reports `type: "invalid_request_error"`, so the classifier
  // reaches that list and returns terminal before anything can ask whether the next target
  // could have served the request (#4903).
  if (isResponseFormatCapabilityRefusal(status, message, options?.code)) return "hop";
  if (isAnthropicBillingFailure(status, message, options?.code)) return "hop";
  // Kiro's body validator rejects the request for the identity it was routed under. The account
  // layer rotates logins first; this is the no-alternate case, so the next candidate is the
  // only remaining chance to serve the turn. Only a caller that knows the failed target is a Kiro
  // adapter grants it (the words alone prove nothing for another provider); it must precede the
  // generic invalid_request_error stop.
  if (options?.failedAdapter === "kiro" && isKiroAccountProfileRefusal(status, message)) return "hop";
  // A structured account-window exhaustion code is provider-state evidence even inside a
  // 400 / invalid_request_error envelope, so it must precede that stop. Exhaustion prose alone
  // never reaches this arm: a validation error can quote quota wording back (#7625).
  if (hasAccountExhaustionCode(options?.code)) return "hop";
  if (["origin_rejected", "context_length_exceeded", "invalid_request_error"].includes(error.code ?? "")) {
    return "stop";
  }
  // Quota prose inside a request-validation status (400/422) is not authoritative without a
  // structured code: the body can quote prompt or parameter text back (#7625).
  if (error.code === "insufficient_quota" && (status === 400 || status === 422)) return "stop";
  // 402 (payment required) and 425 (too early) are provider-state signals, not verdicts about
  // the request: another candidate can still serve it.
  if ([401, 402, 403, 404, 408, 425, 429].includes(status) || status >= 500) return "hop";
  if ([
    "permission_denied",
    "subscription_required",
    "invalid_api_key",
    "insufficient_quota",
    "payment_required",
    "billing_error",
    "insufficient_balance",
    "rate_limit_exceeded",
    "server_is_overloaded",
    "upstream_server_error",
  ].includes(error.code ?? "")) {
    return "hop";
  }
  return "stop";
}
