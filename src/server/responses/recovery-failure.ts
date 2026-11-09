import { recordUpstreamValidationDetail, type FailureDetail } from "../request-log-failure-detail";
import { carryProxyErrorPresentation } from "../../lib/client-error-body";
import { carryPolicyCandidateRefusal } from "./policy-request-scope";
import { upstreamFailureEvidence, carryProxyError } from "../../lib/client-error-body";
import { carryWorkflowRefusal } from "../workflow-refusal";
import { codexAccountModelRefusal, codexAccountModelRefusalHardStopCode, type CodexAccountModelRefusal } from "../../routing/candidate-failure";
import { parseRetryAfterMs } from "../../lib/retry-after-delay";
import type { ConsumedRecoveryFailure, HandleResponsesOptions } from "./core-options";
import type { OcxUsage } from "../../types";
import { readBoundedResponseBody } from "../../lib/bounded-body";
import { carryAnthropicRefusalCredential } from "../../oauth/anthropic-account-refusal";
import { carryCodexAccountCooldownResponse } from "../../codex/account-cooldown-response";
import {
  isCyberPolicyCode,
  isCyberPolicyMessage,
  CYBER_POLICY_ERROR_CODE,
  CYBER_POLICY_FALLBACK_MESSAGE,
  isModelNotInPlanRefusal,
  markVerifiedModelNotInPlanResponse,
  namesModelNotInPlanCode,
} from "../../lib/errors";
import { normalizeUpstreamErrorText } from "./core-errors";
import { resolveClientRetryAfter } from "../../lib/retry-after";
import { formatErrorResponse } from "../../bridge";
import { trustedUpstreamErrorCode } from "../../bridge/errors";
import { carryReplayRefusal, isNonReplayableResponse, isReplayRefusalResponse, markResponseNonReplayable, UPSTREAM_RESET_REPLAY_REFUSED_CODE } from "../../lib/upstream-retry";
import { MODEL_NOT_ALLOWED_FOR_KEY } from "../admission-model-scope";
import type { ResponsesTerminalStatus } from "../../bridge";

export async function consumeRecoveryFailure(
  response: Response,
  signal?: AbortSignal,
  now = Date.now(),
  logCtx?: { failureDetail?: FailureDetail },
): Promise<ConsumedRecoveryFailure> {
  // Read before the body: the marker lives on this Response object, and the failure below is
  // rebuilt as a new one that would otherwise lose it.
  const nonReplayable = isNonReplayableResponse(response);
  const replayRefusal = isReplayRefusalResponse(response);
  const fallback = `Provider error ${response.status}`;
  let classificationText = fallback;
  let codexModelRefusal: CodexAccountModelRefusal | undefined;
  let upstreamCode: string | undefined;
  let upstreamMessage: string | undefined;
  let upstreamType: string | undefined;
  const serverError = response.status >= 500 && response.status < 600;
  // A local key-scope refusal is already the client's answer: rebuilding it as an upstream_error
  // would hide the stable model_not_allowed_for_key type clients branch on.
  let scopeRefusalBody: string | undefined;
  let verifiedPlanRefusal = false;
  try {
    const legacy = upstreamFailureEvidence(response);
    if (legacy !== undefined) void response.body?.cancel().catch(() => undefined);
    const body = await readBoundedResponseBody(legacy === undefined ? response : new Response(legacy), { signal, reportUtf8Validity: serverError });
    // A 5xx body counts as quota or classification evidence only when it decoded as valid
    // UTF-8, matching shouldRetryCodexPoolAccountQuota. A malformed byte keeps the status-only
    // fallback, with one exception: a cyber-policy refusal must still prevent recovery, so the
    // replacement-decoded text may carry that verdict and nothing else.
    const utf8Trusted = !serverError || body.utf8Valid === true;
    if (body.displaySafe && !body.truncated) {
      if (logCtx) recordUpstreamValidationDetail(logCtx, response.status, body.text);
      if (response.status === 400) codexModelRefusal = codexAccountModelRefusal(response.status, body.text);
      const normalized = normalizeUpstreamErrorText(body.text, fallback);
      if (utf8Trusted || isCyberPolicyCode(normalized.code) || isCyberPolicyMessage(normalized.safeText)) {
        classificationText = normalized.safeText;
        upstreamCode = codexAccountModelRefusalHardStopCode(response.status, body.text)
          ?? trustedUpstreamErrorCode(response, normalized.code);
        // Only the adapter's own normalized refusal may claim the plan-refusal hop; another
        // upstream echoing the code in any spelling keeps the meaning it had before, which was none.
        if (namesModelNotInPlanCode(upstreamCode) && !isModelNotInPlanRefusal(response, upstreamCode)) upstreamCode = undefined;
        verifiedPlanRefusal = isModelNotInPlanRefusal(response, upstreamCode);
        upstreamMessage = normalized.message;
        upstreamType = normalized.type;
      }
      if (nonReplayable && response.status === 403 && upstreamType === MODEL_NOT_ALLOWED_FOR_KEY) {
        scopeRefusalBody = body.text;
      }
    }
  } catch (error) {
    if (signal?.aborted) throw error;
    classificationText = fallback;
  }
  const cyberFailure = isCyberPolicyCode(upstreamCode) || isCyberPolicyMessage(classificationText);
  const normalizedUpstreamCode = cyberFailure ? CYBER_POLICY_ERROR_CODE
    : replayRefusal ? UPSTREAM_RESET_REPLAY_REFUSED_CODE : upstreamCode;
  const message = cyberFailure
    ? upstreamMessage
      ?? (isCyberPolicyCode(upstreamCode) ? CYBER_POLICY_FALLBACK_MESSAGE : classificationText)
    : classificationText === fallback
      ? fallback
      : `${fallback}: ${classificationText}`;
  const upstreamRetryAfter = response.headers.get("retry-after");
  // Past HTTP dates are an immediate retry directive, just like the numeric value zero.
  // Normalize before the client helper discards them and substitutes a default delay.
  const effectiveRetryAfter = parseRetryAfterMs(upstreamRetryAfter, now) === undefined
    && parseRetryAfterMs(upstreamRetryAfter, now, { preserveImmediate: true }) !== undefined
    ? "0"
    : upstreamRetryAfter;
  // Client response may get the synthetic "2" fallback; cooldown metadata must not —
  // otherwise coolComboTarget treats it as a 2s cooldown instead of the 60s default.
  const clientRetryAfter = replayRefusal ? undefined : resolveClientRetryAfter({
    status: response.status,
    message,
    upstreamRetryAfter: effectiveRetryAfter,
    now,
  });
  const failureResponse = scopeRefusalBody !== undefined
    ? new Response(scopeRefusalBody, { status: 403, headers: { "Content-Type": "application/json" } })
    : formatErrorResponse(
    response.status,
    cyberFailure ? (upstreamType ?? CYBER_POLICY_ERROR_CODE) : "upstream_error",
    message,
    {
      ...(normalizedUpstreamCode !== undefined ? { code: normalizedUpstreamCode } : {}),
      ...(clientRetryAfter !== undefined ? { retryAfter: clientRetryAfter } : {}),
    },
  );
  if (nonReplayable) markResponseNonReplayable(failureResponse);
  carryProxyError(response, failureResponse);
  carryWorkflowRefusal(response, failureResponse);
  carryReplayRefusal(response, failureResponse);
  carryProxyErrorPresentation(response, failureResponse);
  carryPolicyCandidateRefusal(response, failureResponse);
  if (verifiedPlanRefusal) markVerifiedModelNotInPlanResponse(failureResponse);
  carryAnthropicRefusalCredential(response, failureResponse);
  carryCodexAccountCooldownResponse(response, failureResponse);
  return {
    response: failureResponse,
    ...(nonReplayable ? { nonReplayable: true } : {}),
    ...(verifiedPlanRefusal ? { verifiedPlanRefusal: true } : {}),
    classificationText,
    ...(codexModelRefusal !== undefined ? { codexModelRefusal } : {}),
    ...(normalizedUpstreamCode !== undefined ? { upstreamCode: normalizedUpstreamCode } : {}),
    ...(upstreamMessage !== undefined ? { upstreamMessage } : {}),
    ...(upstreamType !== undefined ? { upstreamType } : {}),
  };
}

export function createChildPassthroughCallbackGate(options: HandleResponsesOptions) {
  type Pending =
    | { kind: "terminal"; status: ResponsesTerminalStatus }
    | { kind: "cancel" };
  let state: "pending" | "committed" | "discarded" = "pending";
  let pending: Pending | undefined;
  let accepted = false;
  let pendingModel: string | undefined;
  let completionAccepted = false;
  let completionRejected = false;
  const publish = (value: Pending): void => {
    if (value.kind === "terminal") options.onNativePassthroughTerminal?.(value.status);
    else options.onNativePassthroughCancel?.();
  };
  const publishCompletion = (): void => {
    if (state !== "committed" || completionRejected || pendingModel === undefined) return;
    const model = pendingModel;
    pendingModel = undefined;
    options.onResponseComplete?.(model);
  };
  const receive = (value: Pending): void => {
    if (state === "discarded" || accepted) return;
    accepted = true;
    if (value.kind === "cancel" || value.status !== "completed") {
      completionRejected = true;
      pendingModel = undefined;
    }
    if (state === "committed") return publish(value);
    pending ??= value;
  };
  return {
    onTerminal: (status: ResponsesTerminalStatus) => receive({ kind: "terminal", status }),
    onCancel: () => receive({ kind: "cancel" }),
    onResponseComplete: (model: string) => {
      if (state === "discarded" || completionRejected || completionAccepted || !model.trim()) return;
      completionAccepted = true;
      pendingModel = model;
      publishCompletion();
    },
    commit: () => {
      if (state !== "pending") return;
      state = "committed";
      if (pending) publish(pending);
      pending = undefined;
      publishCompletion();
    },
    discard: () => {
      state = "discarded";
      pending = undefined;
      pendingModel = undefined;
    },
  };
}
