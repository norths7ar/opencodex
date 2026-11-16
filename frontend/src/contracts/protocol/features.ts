import type { Protocol } from "./contract";

export type FeatureDisposition = "passthrough" | "translated" | "degraded" | "unsupported";

export const PROTOCOL_FEATURES = [
  "request.tools",
  "request.hosted_tools",
  "request.images",
  "request.documents",
  "request.multiple_choices",
  "request.logprobs",
  "request.logit_bias",
  "request.seed",
  "request.audio",
  "request.prediction",
  "request.response_format",
  "request.reasoning",
  "request.thinking_budget",
  "request.top_k",
  "request.cache_control",
  "request.previous_response_id",
  "request.store",
  "request.background",
  "request.compaction",
] as const;
export type ProtocolFeature = (typeof PROTOCOL_FEATURES)[number];

const FEATURE_SET = new Set<string>(PROTOCOL_FEATURES);
export function isProtocolFeature(value: unknown): value is ProtocolFeature {
  return typeof value === "string" && FEATURE_SET.has(value);
}

/** Which public protocols can express each feature at all. */
export const FEATURE_SOURCES: Readonly<Record<ProtocolFeature, readonly Protocol[]>> = {
  "request.tools": ["responses", "chat", "messages"],
  "request.hosted_tools": ["responses"],
  "request.images": ["responses", "chat", "messages"],
  "request.documents": ["responses", "chat", "messages"],
  "request.multiple_choices": ["chat"],
  "request.logprobs": ["responses", "chat"],
  "request.logit_bias": ["chat"],
  "request.seed": ["chat"],
  "request.audio": ["chat"],
  "request.prediction": ["chat"],
  "request.response_format": ["responses", "chat"],
  "request.reasoning": ["responses", "chat", "messages"],
  "request.thinking_budget": ["messages"],
  "request.top_k": ["messages"],
  "request.cache_control": ["messages"],
  "request.previous_response_id": ["responses"],
  "request.store": ["responses"],
  "request.background": ["responses"],
  "request.compaction": ["responses"],
};
