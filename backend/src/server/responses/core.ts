import { handleResponsesInner } from "./core-pipeline";
import type { OcxConfig } from "../../types";
import type { RequestLogContext } from "../request-log";
import type {
  HandleResponsesOptions,
  ResponsesDispatchers,
} from "./core-options";
import { createTranslatorBudget } from "../../lib/translator-budget";
import { captureExplicitOpenAiCallerAuth } from "../../providers/openai-sidecar";
import { captureCallerDirectAuth } from "../../providers/caller-authorization";
import { createInferenceSendBudget } from "../inference/context";
import { finalizeOwnedTranslatorBudget, finalizeAccountLease } from "./core-lifetime";
import { runWithCompactionRecovery } from "./compaction-recovery";
import { captureAdmissionPolicy } from "../capture-admission-policy";
/**
 * Route one `/v1/responses` request through the adapter pipeline: recovery loop, passthrough
 * wire, image/web-search bridges, and the terminal-guard continuation.
 */
export async function handleResponses(
  req: Request,
  config: OcxConfig,
  logCtx: RequestLogContext,
  options: HandleResponsesOptions = {},
): Promise<Response> {
  const ownsBudget = options.translatorBudget === undefined;
  const translatorBudget = options.translatorBudget ?? createTranslatorBudget();
  const abortSignal = options.abortSignal ?? req.signal;
  const accountLoad = { lease: null as import("../../oauth/kiro-account-load").AccountLease | null,
    cancelled: abortSignal.aborted };
  function release() { accountLoad.lease?.release(); accountLoad.lease = null; abortSignal.removeEventListener("abort", cancel); }
  function cancel() { accountLoad.cancelled = true; release(); }
  abortSignal.addEventListener("abort", cancel, { once: true });
  try {
    const response = await runWithCompactionRecovery(req, config, logCtx, {
      ...options, abortSignal,
      sidecarAdmissionPolicy: options.sidecarAdmissionPolicy ?? captureAdmissionPolicy(config, options.admission, options.admissionScope),
      openAiSidecarAuth: options.openAiSidecarAuth === undefined
        ? captureExplicitOpenAiCallerAuth(req.headers, config) : options.openAiSidecarAuth,
      nativeCallerAuth: options.nativeCallerAuth === undefined
        ? captureExplicitOpenAiCallerAuth(req.headers, config) : options.nativeCallerAuth,
      callerDirectAuth: options.callerDirectAuth === undefined
        ? captureCallerDirectAuth(req.headers, config) : options.callerDirectAuth,
      // Capture before a retry wrapper rebuilds the Request headers.
      visionDescribeTerminal: options.visionDescribeTerminal === true
        || req.headers.get("x-opencodex-vision-describe") === "1",
      translatorBudget,
      accountLoad,
      // Install one execution budget at ingress, including the spend observer.
      sendBudget: options.sendBudget ?? createInferenceSendBudget(req, logCtx),
    }, (req, config, logCtx, options) => handleResponsesInner(req, config, logCtx, options, requestDispatchers));
    const finalResponse = ownsBudget ? finalizeOwnedTranslatorBudget(response, translatorBudget, abortSignal) : response;
    if (!accountLoad.lease) { release(); return finalResponse; }
    return finalizeAccountLease(finalResponse, release);
  } catch (error) {
    release();
    if (ownsBudget) translatorBudget.dispose();
    throw error;
  }
}
const requestDispatchers: ResponsesDispatchers = { handleResponses };

export { adapterNeedsForcedContinuation } from "./core-replay";
export {
  sidecarOutcomeRecorder, codexLogAccountId, usesCodexForwardPoolAuth, preAuthUpstreamHostCircuitKey,
  upstreamHostCircuitOpenResponse, shouldRetryCodexPoolAccountQuota, shouldRetryCodexScopedQuotaOnAlternate,
  shouldRetryCodexPoolAccountTransient, codexAccountGatedCanonicalWireModel, codexForwardTerminalOutcomeRecorder,
} from "./core-codex-account";
export { shouldAttemptOpaqueBlobRecovery } from "./core-opaque-recovery";
export { readDisplaySafeErrorText, decodeRequestErrorResponse, clientCancelledResponse } from "./core-errors";
export type { HandleResponsesOptions } from "./core-options";
export { UPSTREAM_JSON_BODY_READ_OPTIONS, linkAbortSignal } from "./core-lifetime";
export { poolCredentialRefreshIncompleteResponse } from "./core-auth";
export { applyServiceTierGate } from "./core-normalize";
export { DEFAULT_SHADOW_SOURCE_MODELS, isShadowSourceModel, shadowSourceModels } from "../../lib/shadow-call";
