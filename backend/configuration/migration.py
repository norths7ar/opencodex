"""Translate a Lite config snapshot without touching disk, process state or credentials."""

from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import JsonValue, ValidationError

from .models import ChannelRequestPolicy, Configuration, CredentialRecord, CustomModel

_OAUTH_SERVICES = {
    "openai": ("https://chatgpt.com/backend-api/codex", "openai-responses", "chatgpt"),
}
_RETIRED_OAUTH_IDS = {"anthropic", "kimi", "kimi-responses"}
_API_SERVICES = {
    ("https://api.openai.com", "key"): "openai",
    ("https://api.openai.com/v1", "key"): "openai",
    ("https://api.deepseek.com", "key"): "deepseek",
    ("https://api.deepseek.com/v1", "key"): "deepseek",
    ("https://api.deepseek.com/anthropic", "key"): "deepseek",
    ("https://api.xiaomimimo.com/v1", "key"): "mimo",
    ("https://api.xiaomimimo.com/anthropic", "key"): "mimo",
    ("https://token-plan-cn.xiaomimimo.com/v1", "key"): "mimo",
    ("https://opencode.ai/zen/v1", "key"): "opencode-zen",
    ("https://opencode.ai/zen/go/v1", "key"): "opencode-go",
}
_METADATA = {
    "contextWindow": "modelContextWindows",
    "inputModalities": "modelInputModalities",
    "outputModalities": "modelOutputModalities",
    "apiCapabilities": "modelApiCapabilities",
    "reasoningEfforts": "modelReasoningEfforts",
    "defaultReasoningEffort": "modelDefaultReasoningEfforts",
    "displayName": "modelDisplayNames",
    "parallelToolCalls": "modelParallelToolCalls",
    "codexToolMode": "modelCodexToolModes",
    "maxInputTokens": "modelMaxInputTokens",
    "maxOutputTokens": "modelMaxOutputTokens",
    "autoCompactTokenLimit": "modelAutoCompactTokenLimits",
    "supportsReasoningSummaries": "modelSupportsReasoningSummaries",
    "supportsVerbosity": "modelSupportsVerbosity",
    "suppressSyntheticMax": "modelSuppressSyntheticMax",
    "supportsServiceTier": "modelSupportsServiceTier",
    "supportsReasoning": "modelSupportsReasoning",
    "supportsReasoningEffort": "modelSupportsReasoningEffort",
    "supportsThinkingToggle": "modelSupportsThinkingToggle",
    "defaultThinkingEnabled": "modelDefaultThinkingEnabled",
}

_REQUEST = {
    "maxInboundBodyBytes": "max_inbound_body_bytes",
    "compactionRouting": "compaction_routing",
    "codexClientCompaction": "codex_client_compaction",
    "fastRows": "fast_rows",
    "cursorEffortRows": "cursor_effort_rows",
    "cacheRetention": "cache_retention",
    "simpleFallback": "simple_fallback",
    "visionSidecar": "vision_sidecar",
    "webSearchSidecar": "web_search_sidecar",
    "workflowBudget": "workflow_budget",
    "spend": "spend",
}
_AGENTS = {
    "effortCap": "effort_cap",
    "subagentEffortCap": "subagent_effort_cap",
    "modelPinnedEfforts": "model_pinned_efforts",
    "subagentModels": "subagent_models",
    "modelPickerOrder": "model_picker_order",
    "modelPickerOrderMode": "model_picker_order_mode",
    "injectionModel": "injection_model",
    "injectionEffort": "injection_effort",
    "syncCodexSubagentDefaults": "sync_codex_subagent_defaults",
    "codexAgentSettings": "codex_agent_settings",
    "subagentModelFallbackByModel": "subagent_model_fallback_by_model",
    "subagentModelFallback": "subagent_model_fallback",
    "subagentModelFallbackPollMs": "subagent_model_fallback_poll_ms",
    "codexRoleTiers": "codex_role_tiers",
}


@dataclass(frozen=True)
class MigrationResult:
    configuration: Configuration
    credentials: dict[str, CredentialRecord] = field(repr=False)
    remainder: dict[str, JsonValue] = field(repr=False)
    retired_fields: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.remainder


def _extract(source, mapping):
    return {target: source.pop(key) for key, target in mapping.items() if key in source}


def _pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _remaining(remainder, path, values):
    for key, value in values.items():
        remainder[f"{path}/{_pointer(key)}"] = value


def _channel_metadata(row, remainder, path):
    metadata = _extract(
        row,
        {
            "modelCosts": "costs",
            "presetMetadataVersion": "preset_version",
            "modelPreset": "preset",
            "initialModelSelection": "initial_selection",
            "newModelPolicy": "new_model_policy",
        },
    )
    metadata["defaults"] = _extract(row, {key: key for key in _METADATA})
    overrides = {}
    for key, mapping in _METADATA.items():
        if mapping not in row:
            continue
        values = row.pop(mapping)
        if values is None:
            metadata.setdefault("null_override_maps", []).append(key)
            continue
        if not isinstance(values, dict):
            remainder[f"{path}/{mapping}"] = values
            continue
        if not values:
            metadata.setdefault("cleared_override_maps", []).append(key)
        for model_id, value in values.items():
            overrides.setdefault(model_id, {})[key] = value
    metadata["overrides"] = overrides
    raw_capabilities = row.pop("modelCapabilities", {})
    if not isinstance(raw_capabilities, dict):
        remainder[f"{path}/modelCapabilities"] = raw_capabilities
        return metadata
    capabilities = {}
    for model_id, declarations in raw_capabilities.items():
        cap_path = f"{path}/modelCapabilities/{_pointer(model_id)}"
        if not isinstance(declarations, dict):
            remainder[cap_path] = declarations
            continue
        typed = _extract(declarations, {key: key for key in _METADATA})
        _remaining(remainder, cap_path, declarations)
        if typed or not declarations:
            capabilities[model_id] = typed
    metadata["capabilities"] = capabilities
    return metadata


def _channel(row, index, remainder):
    path = f"/providers/{index}"
    if not isinstance(row, dict):
        raise ValueError(f"Expected a provider object at {path}.")
    if row.pop("apiKey", None) not in (None, ""):
        raise ValueError(f"Plaintext persisted API credential at {path}/apiKey cannot be migrated.")
    credential = _extract(
        row,
        {
            "apiKeyEnv": "env_var",
            "protectedKey": "protected_key",
            "headers": "headers",
            "keyLabel": "key_label",
        },
    )
    # Public projections add hasKey; they are not safe migration inputs.
    if "hasKey" in row:
        raise ValueError("Use the saved Lite configuration, not the public configuration view.")
    channel = _extract(
        row,
        {
            "id": "id",
            "name": "name",
            "note": "note",
            "allowPrivateNetwork": "allow_private_network",
            "requestPacing": "request_pacing",
            "preserveResponsesReasoningContent": "preserve_responses_reasoning_content",
            "disabled": "disabled",
            "alias": "alias",
            "apiKeyTransport": "api_key_transport",
        },
    )
    pacing = channel.get("request_pacing")
    if isinstance(pacing, dict) and pacing.get("keySelection") == "round-robin":
        remainder[f"{path}/requestPacing"] = channel.pop("request_pacing")
    protocol = row.pop("adapter", "openai-responses" if row.get("responses") else "openai-chat")
    row.pop("responses", None)
    auth_mode = row.pop("authMode", "key")
    channel.update(protocol=protocol, auth_mode=auth_mode)
    endpoints = _extract(
        row, {"baseUrl": "base_url", "responsesPath": "responses", "chatCompletionsPath": "chat"}
    )
    base = endpoints.get("base_url")
    base = base.strip().rstrip("/") if isinstance(base, str) else ""
    if auth_mode == "oauth":
        expected = _OAUTH_SERVICES.get(channel.get("id"))
        if not expected or (base, protocol) != expected[:2]:
            raise ValueError(f"Unrecognized OAuth channel binding at {path}.")
        service_id = expected[2]
        channel["oauth_binding"] = {"service_id": service_id, "account_ref": service_id}
    else:
        service_id = _API_SERVICES.get((base, str(auth_mode)), "custom")
    channel.update(service_id=service_id, endpoints=endpoints)
    models = _extract(
        row,
        {
            "defaultModel": "default",
            "selectedModels": "selected",
            "models": "static",
            "liveModels": "live",
            "modelAliases": "aliases",
            "defaultAliases": "default_aliases",
            "modelAdapters": "adapters",
        },
    )
    models["metadata"] = _channel_metadata(row, remainder, path)
    channel["models"] = models
    channel["request_policy"] = _extract(
        row, {key: key for key in ChannelRequestPolicy.model_fields}
    )
    _remaining(remainder, path, row)
    return channel, credential


def migrate_legacy(raw: dict) -> MigrationResult:
    """Return version 2 configuration, private credentials and unmapped JSON pointers."""
    if not isinstance(raw, dict) or "schema_version" in raw:
        raise ValueError("Expected an unversioned Lite configuration object.")
    source = deepcopy(raw)
    retired = []
    for name in (
        "multiAgentMode",
        "keepNativeChatGptOnV1",
        "multiAgentGuidanceEnabled",
        "injectionPrompt",
        "multiAgentSurfaceAdvisoryVersion",
        "multiAgentSurfaceAdvisoryAcknowledged",
    ):
        if name in source:
            source.pop(name)
            retired.append(f"/{name}")
    settings = source.get("codexAgentSettings")
    if isinstance(settings, dict):
        for name in ("enabled", "subagentDeveloperInstructions", "multiAgentModeHintText"):
            if name in settings:
                settings.pop(name)
                retired.append(f"/codexAgentSettings/{name}")
    providers = source.pop("providers", [])
    if not isinstance(providers, list):
        raise ValueError("Expected Lite's providers list, not an original OpenCodex provider map.")
    remainder = {}
    channels = []
    secrets = {}
    retired_ids = set()
    for index, row in enumerate(providers):
        if (
            isinstance(row, dict)
            and row.get("authMode") == "oauth"
            and isinstance(row.get("id"), str)
            and row["id"] in _RETIRED_OAUTH_IDS
        ):
            retired_ids.add(row["id"])
            retired.append(f"/providers/{index}")
            continue
        channel, credential = _channel(row, index, remainder)
        channels.append(channel)
        if credential and isinstance(channel.get("id"), str):
            secrets[channel["id"]] = credential
    config = _extract(
        source, {"defaultProvider": "default_channel_id", "defaultModel": "default_model_id"}
    )
    if (
        retired_ids
        and isinstance(config.get("default_channel_id"), str)
        and (
            config["default_channel_id"] in retired_ids
            and not any(channel.get("id") == config["default_channel_id"] for channel in channels)
        )
    ):
        retired.append("/defaultProvider")
        if "default_model_id" in config:
            retired.append("/defaultModel")
        config.update(default_channel_id=None, default_model_id=None)
    config["schema_version"] = 2
    config["channels"] = channels
    config["models"] = _extract(
        source,
        {
            "disabledModels": "legacy_disabled",
            "customModels": "custom",
            "defaultModelAliases": "default_aliases",
            "modelDiscovery": "discovery",
            "providerContextCaps": "context_caps",
            "providerContextCapValues": "context_cap_values",
            "contextCapValue": "context_cap_value",
        },
    )
    custom = config["models"].get("custom", [])
    if isinstance(custom, list):
        for index, row in enumerate(custom):
            if isinstance(row, dict):
                custom[index] = _extract(row, {key: key for key in CustomModel.model_fields})
                _remaining(remainder, f"/customModels/{index}", row)
    usage = _extract(source, {"managementUsageMaxReadBytes": "management_max_read_bytes"})
    if "apiKeys" in source:
        keys = source.pop("apiKeys")
        if isinstance(keys, list) and all(isinstance(item, dict) and "id" in item for item in keys):
            usage["key_ids"] = [item.pop("id") for item in keys]
            for index, item in enumerate(keys):
                _remaining(remainder, f"/apiKeys/{index}", item)
        else:
            remainder["/apiKeys"] = keys
    config["features"] = {
        "request": _extract(source, _REQUEST),
        "agents": _extract(source, _AGENTS),
        "usage": usage,
    }
    _remaining(remainder, "", source)
    try:
        configuration = Configuration.model_validate(config)
        credentials = {
            identity: CredentialRecord.model_validate(value) for identity, value in secrets.items()
        }
    except ValidationError as exc:
        # Do not permit exception context or Pydantic's input_value to emit credential material.
        locations = ", ".join(
            ".".join(map(str, item["loc"]))
            for item in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ValueError(
            f"Invalid migrated configuration fields: {locations or 'selection'}."
        ) from None
    return MigrationResult(configuration, credentials, remainder, tuple(retired))
