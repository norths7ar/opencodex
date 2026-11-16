// Generated from backend.api.schema and the channel configuration editor schema.

export type CredentialStatus = {
  state: "unknown" | "configured" | "missing" | "reauth_required" | "unavailable";
  label?: string | null;
};

export type Endpoints = {
  base_url: string;
  discovery?: string | null;
  chat?: string | null;
  responses?: string | null;
  messages?: string | null;
};

export type ChannelView = {
  id: string;
  service_id: string;
  name: string;
  protocol: "openai-chat" | "openai-responses" | "anthropic";
  auth_mode: "key" | "local" | "oauth";
  endpoints: Endpoints;
  disabled: boolean;
  alias?: string | null;
  credential: CredentialStatus;
  has_custom_headers: boolean;
};

export type Channel = {
  id: string;
  service_id: string;
  name: string;
  allow_private_network?: boolean;
  request_pacing?: RequestPacing | null;
  preserve_responses_reasoning_content?: boolean | null;
  note?: string | null;
  protocol: "openai-chat" | "openai-responses" | "anthropic";
  auth_mode: "key" | "local" | "oauth";
  oauth_binding?: OAuthBinding | null;
  endpoints: Endpoints;
  api_key_transport?: "bearer" | "x-api-key" | null;
  disabled?: boolean;
  alias?: string | null;
  models?: ChannelModels;
  request_policy?: ChannelRequestPolicy;
};

export type ChannelModelMetadata = {
  defaults?: ModelMetadata;
  overrides?: Record<string, ModelMetadata>;
  cleared_override_maps?: Array<string>;
  null_override_maps?: Array<string>;
  capabilities?: Record<string, ModelMetadata>;
  costs?: Record<string, Record<string, JsonValue> | null>;
  preset_version?: number | null;
  preset?: Record<string, JsonValue> | null;
  initial_selection?: Record<string, JsonValue> | null;
  new_model_policy?: string | null;
};

export type ChannelModels = {
  default?: string | null;
  selected?: Array<string>;
  static?: Array<string>;
  live?: boolean;
  aliases?: Record<string, string>;
  default_aliases?: boolean | null;
  adapters?: Record<string, "openai-chat" | "openai-responses" | "anthropic">;
  metadata?: ChannelModelMetadata;
};

export type ChannelRequestPolicy = {
  defaultMaxOutputTokens?: number | null;
  hideThinkingSummary?: boolean | null;
  statelessResponses?: boolean | null;
  supportsResponsesCustomTools?: boolean | null;
  supportsNativeCompactEndpoint?: boolean | null;
  supportsNativeCompactionTrigger?: boolean | null;
  foldDeveloperRoleToSystem?: boolean | null;
  preserveReasoningContentModels?: Array<string> | null;
  modelSuffixBracketStrip?: boolean | null;
  requiresAdjacentResponsesToolResults?: boolean | null;
  noTemperatureModels?: Array<string> | null;
  noTopPModels?: Array<string> | null;
  noPenaltyModels?: Array<string> | null;
  autoToolChoiceOnlyModels?: Array<string> | null;
  promptCacheKey?: boolean | null;
  thinkingToggleModels?: Array<string> | null;
  noReasoningModels?: Array<string> | null;
  noVisionModels?: Array<string> | null;
  reasoningEffortMap?: Record<string, string> | null;
  modelReasoningEffortMap?: Record<string, Record<string, string>> | null;
  pinnedReasoningEffort?: string | null;
  modelPinnedReasoningEfforts?: Record<string, string> | null;
  modelDiscoveryClientVersion?: string | null;
};

export type JsonValue =
  null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type ModelMetadata = {
  contextWindow?: number | null;
  inputModalities?: Array<string> | null;
  outputModalities?: Array<string> | null;
  apiCapabilities?: Record<string, JsonValue> | null;
  reasoningEfforts?: Array<string> | null;
  defaultReasoningEffort?: string | null;
  displayName?: string | null;
  parallelToolCalls?: boolean | null;
  codexToolMode?: string | null;
  maxInputTokens?: number | null;
  maxOutputTokens?: number | null;
  autoCompactTokenLimit?: number | null;
  supportsReasoningSummaries?: boolean | null;
  supportsVerbosity?: boolean | null;
  suppressSyntheticMax?: boolean | null;
  supportsServiceTier?: boolean | null;
  supportsReasoning?: boolean | null;
  supportsReasoningEffort?: boolean | null;
  supportsThinkingToggle?: boolean | null;
  defaultThinkingEnabled?: boolean | null;
};

export type OAuthBinding = {
  service_id: string;
  account_ref: string;
};

export type PacingRule = {
  requestsPerMinute?: number | null;
  minIntervalMs?: number | null;
  maxConcurrentRequests?: number | null;
};

export type RequestPacing = {
  enabled: boolean;
  requestsPerMinute?: number | null;
  minIntervalMs?: number | null;
  maxConcurrentRequests?: number | null;
  models?: Record<string, PacingRule>;
  keys?: PacingRule | null;
  keySelection?: "active" | null;
};

export type ApiError = {
  code: string;
  message: string;
  field?: string | null;
  retryable: boolean;
};

export type ChannelDetail = {
  revision: number;
  channel: Channel;
  credential: CredentialStatus;
  has_custom_headers: boolean;
  discovery: {
    state: "not_checked" | "ready" | "refreshing" | "failed" | "unsupported";
    last_success_at?: string | null;
    last_attempt_at?: string | null;
    stale_default_model?: string | null;
    stale_aliases?: Record<string, string>;
    error?: ApiError | null;
  };
};

export type ChannelDirectory = {
  revision: number;
  default_channel_id: string | null;
  default_model_id: string | null;
  channels: ChannelDetail[];
};

export type ClearCredential = {
  action: "clear";
};

export type KeepCredential = {
  action?: "keep";
};

export type SetApiKey = {
  action: "set_key";
  value: string;
};

export type SetEnvironmentCredential = {
  action: "set_environment";
  variable: string;
};

export type SaveChannel = {
  channel: Channel;
  credential?: KeepCredential | SetApiKey | SetEnvironmentCredential | ClearCredential;
  headers?: Record<string, string> | null;
};

export type ChannelConfigurationEdit = {
  revision: number;
  channels: Array<Channel>;
  default_channel_id?: string | null;
  default_model_id?: string | null;
};

export type ClientApplicationOutcome = {
  ok: boolean;
  state: string;
  message?: string | null;
  reason?: string | null;
};

export type ApplicationReceipt = {
  status: "applied" | "pending" | "skipped" | "failed";
  clients?: Record<string, ClientApplicationOutcome> | null;
  message?: string | null;
};
export type ChannelSaved = ChannelDetail & { saved: true; application: ApplicationReceipt };
