"""Persistent, versioned configuration; separate from secrets and management DTOs."""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ChannelId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Protocol = Literal["openai-chat", "openai-responses", "anthropic"]
AuthMode = Literal["key", "local", "oauth"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Endpoints(Schema):
    """Overrides are base-URL suffixes in migrated configuration."""

    base_url: str
    discovery: str | None = None
    chat: str | None = None
    responses: str | None = None
    messages: str | None = None

    @field_validator("discovery", "chat", "responses", "messages")
    @classmethod
    def validate_suffix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or any(ord(char) < 32 for char in value):
            raise ValueError("Endpoint overrides must be base URL suffixes beginning with '/'.")
        if urlsplit(value).fragment:
            raise ValueError("Endpoint overrides cannot contain a fragment.")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected an HTTP(S) base URL without credentials, query or fragment.")
        return normalized


class ModelMetadata(Schema):
    contextWindow: int | None = Field(default=None, gt=0)
    inputModalities: list[str] | None = None
    outputModalities: list[str] | None = None
    apiCapabilities: dict[str, JsonValue] | None = None
    reasoningEfforts: list[str] | None = None
    defaultReasoningEffort: str | None = None
    displayName: str | None = None
    parallelToolCalls: bool | None = None
    codexToolMode: str | None = None
    maxInputTokens: int | None = Field(default=None, gt=0)
    maxOutputTokens: int | None = Field(default=None, gt=0)
    autoCompactTokenLimit: int | None = Field(default=None, gt=0)
    supportsReasoningSummaries: bool | None = None
    supportsVerbosity: bool | None = None
    suppressSyntheticMax: bool | None = None
    supportsServiceTier: bool | None = None
    supportsReasoning: bool | None = None
    supportsReasoningEffort: bool | None = None
    supportsThinkingToggle: bool | None = None
    defaultThinkingEnabled: bool | None = None

    @field_validator("inputModalities", "outputModalities")
    @classmethod
    def validate_modalities(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(
            kind not in {"text", "image", "audio", "video"} for kind in value
        ):
            raise ValueError("Unsupported model modality.")
        return value

    @field_validator("displayName")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Display name must be nonempty.")
        return value


class ChannelModelMetadata(Schema):
    defaults: ModelMetadata = Field(default_factory=ModelMetadata)
    overrides: dict[str, ModelMetadata] = Field(default_factory=dict)
    cleared_override_maps: list[str] = Field(default_factory=list)
    null_override_maps: list[str] = Field(default_factory=list)
    capabilities: dict[str, ModelMetadata] = Field(default_factory=dict)
    costs: dict[str, dict[str, JsonValue] | None] = Field(default_factory=dict)
    preset_version: int | None = None
    preset: dict[str, JsonValue] | None = None
    initial_selection: dict[str, JsonValue] | None = None
    new_model_policy: str | None = None


class PacingRule(Schema):
    requestsPerMinute: float | None = Field(default=None, ge=1 / 60, le=60_000)
    minIntervalMs: int | None = Field(default=None, ge=1, le=3_600_000)
    maxConcurrentRequests: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def has_limit(self):
        if all(
            value is None
            for value in (self.requestsPerMinute, self.minIntervalMs, self.maxConcurrentRequests)
        ):
            raise ValueError("A pacing rule requires at least one limit.")
        return self


class RequestPacing(Schema):
    enabled: bool
    requestsPerMinute: float | None = Field(default=None, ge=1 / 60, le=60_000)
    minIntervalMs: int | None = Field(default=None, ge=1, le=3_600_000)
    maxConcurrentRequests: int | None = Field(default=None, ge=1)
    models: dict[str, PacingRule] = Field(default_factory=dict)
    keys: PacingRule | None = None
    keySelection: Literal["active"] | None = None

    @model_validator(mode="after")
    def has_limit(self):
        if any(not key.strip() for key in self.models):
            raise ValueError("Pacing model IDs must be nonblank.")
        if self.enabled and not (
            self.requestsPerMinute
            or self.minIntervalMs
            or self.maxConcurrentRequests
            or self.models
            or self.keys
        ):
            raise ValueError("Enabled pacing requires at least one limit.")
        return self


class ChannelRequestPolicy(Schema):
    defaultMaxOutputTokens: int | None = None
    hideThinkingSummary: bool | None = None
    statelessResponses: bool | None = None
    supportsResponsesCustomTools: bool | None = None
    annotateEmptyToolOutputs: bool | None = None
    supportsNativeCompactEndpoint: bool | None = None
    supportsNativeCompactionTrigger: bool | None = None
    foldDeveloperRoleToSystem: bool | None = None
    preserveReasoningContentModels: list[str] | None = None
    modelSuffixBracketStrip: bool | None = None
    requiresAdjacentResponsesToolResults: bool | None = None
    noTemperatureModels: list[str] | None = None
    noTopPModels: list[str] | None = None
    noPenaltyModels: list[str] | None = None
    autoToolChoiceOnlyModels: list[str] | None = None
    promptCacheKey: bool | None = None
    thinkingToggleModels: list[str] | None = None
    noReasoningModels: list[str] | None = None
    noVisionModels: list[str] | None = None
    reasoningEffortMap: dict[str, str] | None = None
    modelReasoningEffortMap: dict[str, dict[str, str]] | None = None
    pinnedReasoningEffort: str | None = None
    modelPinnedReasoningEfforts: dict[str, str] | None = None
    modelDiscoveryClientVersion: str | None = None


class ChannelModels(Schema):
    default: str | None = None
    selected: list[str] = Field(default_factory=list)
    static: list[str] = Field(default_factory=list)
    live: bool = True
    aliases: dict[str, str] = Field(default_factory=dict)
    default_aliases: bool | None = None
    adapters: dict[str, Protocol] = Field(default_factory=dict)
    metadata: ChannelModelMetadata = Field(default_factory=ChannelModelMetadata)

    @field_validator("aliases", "adapters")
    @classmethod
    def validate_model_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() for key in value):
            raise ValueError("Model IDs must be nonblank.")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", alias) for alias in value.values()):
            raise ValueError("Invalid model alias.")
        if len({alias.lower() for alias in value.values()}) != len(value):
            raise ValueError("Model aliases must be unique.")
        return value


class OAuthBinding(Schema):
    service_id: ChannelId
    account_ref: ChannelId


class Channel(Schema):
    id: ChannelId
    service_id: ChannelId
    name: Annotated[str, Field(min_length=1, max_length=100)]
    allow_private_network: bool = False
    request_pacing: RequestPacing | None = None
    preserve_responses_reasoning_content: bool | None = None
    note: str | None = None
    protocol: Protocol
    auth_mode: AuthMode
    oauth_binding: OAuthBinding | None = None
    endpoints: Endpoints
    api_key_transport: Literal["bearer", "x-api-key"] | None = None
    disabled: bool = False
    alias: str | None = None
    models: ChannelModels = Field(default_factory=ChannelModels)
    request_policy: ChannelRequestPolicy = Field(default_factory=ChannelRequestPolicy)

    def protocol_for_model(self, model_id: str) -> Protocol:
        return self.models.adapters.get(model_id, self.protocol)

    @model_validator(mode="after")
    def validate_oauth_binding(self):
        if (self.auth_mode == "oauth") != (self.oauth_binding is not None):
            raise ValueError("An OAuth channel requires an explicit account binding.")
        return self


class CustomModel(ModelMetadata):
    id: str
    provider: ChannelId
    modelId: str
    addedAt: str | None = None


class GlobalModels(Schema):
    disabled: list[str] = Field(default_factory=list)
    legacy_disabled: list[str] = Field(default_factory=list)
    legacy_selector_collisions: dict[ChannelId, list[str]] = Field(default_factory=dict)
    custom: list[CustomModel] = Field(default_factory=list)
    default_aliases: bool = False
    discovery: dict[str, JsonValue] | None = None
    context_caps: dict[str, int] | None = None
    context_cap_values: dict[str, int] | None = None
    context_cap_value: int | None = None


class RequestSettings(Schema):
    max_inbound_body_bytes: int | None = None

    @field_validator("max_inbound_body_bytes", mode="before")
    @classmethod
    def valid_inbound_limit(cls, value):
        return value if type(value) is int and value >= 0 else None

    compaction_routing: dict[str, JsonValue] | None = None
    codex_client_compaction: bool = False
    fast_rows: bool = True
    cursor_effort_rows: bool = False
    cache_retention: str | None = None
    simple_fallback: dict[str, JsonValue] | None = None
    vision_sidecar: dict[str, JsonValue] | None = None
    web_search_sidecar: dict[str, JsonValue] | None = None
    workflow_budget: dict[str, JsonValue] | None = None
    spend: dict[str, JsonValue] | None = None


class AgentSettings(Schema):
    effort_cap: str | None = None
    subagent_effort_cap: str | None = None
    model_pinned_efforts: dict[str, str] = Field(default_factory=dict)
    subagent_models: list[str] = Field(default_factory=list)
    model_picker_order: list[str] = Field(default_factory=list)
    model_picker_order_mode: str | None = None
    injection_model: str | None = None
    injection_effort: str | None = None
    sync_codex_subagent_defaults: bool = False
    codex_agent_settings: dict[str, JsonValue] = Field(default_factory=dict)
    subagent_model_fallback_by_model: dict[str, list[str]] = Field(default_factory=dict)
    subagent_model_fallback: list[str] = Field(default_factory=list)
    subagent_model_fallback_poll_ms: int = 60_000
    codex_role_tiers: dict[str, JsonValue] = Field(default_factory=dict)


class UsageSettings(Schema):
    management_max_read_bytes: int | None = None
    key_ids: list[str] = Field(default_factory=list)


class Features(Schema):
    request: RequestSettings = Field(default_factory=RequestSettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    usage: UsageSettings = Field(default_factory=UsageSettings)


class Configuration(Schema):
    schema_version: Literal[2] = 2
    channels: list[Channel] = Field(default_factory=list)
    default_channel_id: ChannelId | None = None
    default_model_id: str | None = None
    models: GlobalModels = Field(default_factory=GlobalModels)
    features: Features = Field(default_factory=Features)

    @model_validator(mode="after")
    def validate_selection(self):
        ids = [channel.id for channel in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("Channel IDs must be unique.")
        if self.default_channel_id and self.default_channel_id not in ids:
            raise ValueError("The default channel must exist.")
        if self.default_model_id and not self.default_channel_id:
            raise ValueError("A default model requires a default channel.")
        return self


class CredentialRecord(Schema):
    env_var: str | None = None
    protected_key: str | None = Field(default=None, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    key_label: str | None = None

    @field_validator("env_var")
    @classmethod
    def validate_env_var(cls, value: str | None) -> str | None:
        if value and (
            not value.isascii() or not value.replace("_", "a").isalnum() or value[0].isdigit()
        ):
            raise ValueError("Expected a credential environment variable name.")
        return value or None
