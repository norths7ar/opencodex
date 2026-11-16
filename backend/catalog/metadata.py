"""Resolve model metadata without consulting network or pricing services."""

from dataclasses import dataclass
from typing import Any

from backend.configuration.models import Channel, CustomModel, ModelMetadata

_FIELDS = ModelMetadata.model_fields
_ALIASES = {
    "name": "displayName",
    "display_name": "displayName",
    "context_window": "contextWindow",
    "max_input_tokens": "maxInputTokens",
    "max_output_tokens": "maxOutputTokens",
    "input_modalities": "inputModalities",
    "output_modalities": "outputModalities",
    "api_capabilities": "apiCapabilities",
    "supports_parallel_tool_calls": "parallelToolCalls",
    "default_reasoning_level": "defaultReasoningEffort",
}


def upstream_metadata(row: dict) -> dict:
    result = {field: row[field] for field in _FIELDS if field in row}
    result.update({field: row[key] for key, field in _ALIASES.items() if row.get(key) is not None})
    effort = row.get("effort")
    if isinstance(effort, dict):
        if isinstance(effort.get("supported_levels"), list):
            result["reasoningEfforts"] = effort["supported_levels"]
        if effort.get("default_level"):
            result["defaultReasoningEffort"] = effort["default_level"]
    if isinstance(levels := row.get("supported_reasoning_levels"), list):
        result["reasoningEfforts"] = [
            level["effort"] for level in levels if isinstance(level, dict) and level.get("effort")
        ]
    for field, value in list(result.items()):
        if value is None and field != "apiCapabilities":
            continue
        try:
            ModelMetadata.model_validate({field: value})
        except ValueError:
            del result[field]
            continue
        if (field == "apiCapabilities" and value is None) or (
            field in {"contextWindow", "maxInputTokens", "maxOutputTokens"} and value <= 0
        ):
            del result[field]
    return result


@dataclass(frozen=True, slots=True)
class ResolvedMetadata:
    values: dict[str, Any]
    sources: dict[str, dict]
    inherited: dict[str, Any]
    overrides: dict[str, Any]


def _declared(metadata: ModelMetadata | None) -> dict:
    return metadata.model_dump(exclude_unset=True, include=set(_FIELDS)) if metadata else {}


def resolve_metadata(
    channel: Channel,
    native_id: str,
    *,
    upstream: dict | None = None,
    upstream_provenance: dict | None = None,
    official: dict | None = None,
    preset: ModelMetadata | None = None,
    custom: CustomModel | None = None,
) -> ResolvedMetadata:
    """Explicit null masks lower layers, unlike an omitted field on a typed model."""
    declarations = channel.models.metadata
    official = official or {}
    preset_values = _declared(preset)
    model_values = {
        **_declared(declarations.overrides.get(native_id)),
        **_declared(declarations.capabilities.get(native_id)),
    }
    if declarations.preset_version != 2:
        model_values = {
            field: value
            for field, value in model_values.items()
            if field not in preset_values or value != preset_values[field]
        }
    overrides = {**model_values, **_declared(custom)}
    inherited = {}
    sources = {}
    for values, source in (
        (preset_values, {"source": "preset"}),
        (official, {"source": "official", **official.get("provenance", {})}),
        (upstream_metadata(upstream or {}), {"source": "upstream", **(upstream_provenance or {})}),
        (_declared(declarations.defaults), {"source": "provider"}),
    ):
        for field in _FIELDS.keys() & values.keys():
            inherited[field] = values[field]
            sources[field] = dict(source)
    values = {**inherited, **overrides}
    sources.update({field: {"source": "operator"} for field in overrides})
    return ResolvedMetadata(
        {key: value for key, value in values.items() if value is not None},
        {key: sources[key] for key, value in values.items() if value is not None},
        {key: value for key, value in inherited.items() if value is not None},
        overrides,
    )
