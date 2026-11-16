"""Per-turn effort policy ported from server/effort-policy.ts.

Model pins are explicit overrides. Caps only constrain native V2 collaboration
and spawned child turns; maintenance requests retain their own effort policy.
"""

import json

from .collaboration import collaboration_surface

EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DECLARED_EFFORTS = ("none", "minimal", *EFFORTS)


def model_value(values, model):
    if not isinstance(values, dict):
        return None
    if model in values:
        return values[model]
    family, separator, _ = model.partition(":")
    if separator and family in values:
        return values[family]
    return next((value for key, value in values.items() if key.lower() == model.lower()), None)


def turn_metadata(headers):
    try:
        value = json.loads(headers.get("x-codex-turn-metadata", "{}"))
        return value if isinstance(value, dict) else {}
    except ValueError, TypeError:
        return {}


def is_child(headers):
    return (
        headers.get("x-openai-subagent") == "collab_spawn"
        or turn_metadata(headers).get("subagent_kind") == "thread_spawn"
    )


def capped_effort(cap, supported):
    if supported is None:
        return cap
    ranked = [effort for effort in supported if effort in EFFORTS]
    if not ranked:
        return cap if supported else None
    eligible = [effort for effort in ranked if EFFORTS.index(effort) <= EFFORTS.index(cap)]
    return max(eligible, key=EFFORTS.index) if eligible else None


def apply_effort_policy(body, headers, config, provider, model, protocol, metadata=None):
    if protocol not in {"responses", "chat/completions"}:
        return None
    selector = body.get("model") or model
    provider_data = provider.request_policy.model_dump()
    pins = config.features.agents.model_pinned_efforts
    pin = (
        model_value(provider_data.get("modelPinnedReasoningEfforts"), model)
        or model_value(provider_data.get("modelPinnedReasoningEfforts"), selector)
        or provider_data.get("pinnedReasoningEffort")
        or model_value(pins, selector)
        or model_value(pins, f"{provider.id}/{model}")
        or model_value(pins, model)
    )
    reasoning = body.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    requested = reasoning.get("effort") if protocol == "responses" else body.get("reasoning_effort")
    effective = pin if pin in DECLARED_EFFORTS else requested
    child = is_child(headers)
    maintenance = turn_metadata(headers).get("request_kind") == "compaction"
    applies = not maintenance and (child or collaboration_surface(body) == "v2")
    caps = [config.features.agents.effort_cap]
    if child:
        caps.append(config.features.agents.subagent_effort_cap)
    caps = [cap for cap in caps if cap in EFFORTS]
    if applies and caps:
        supported = model_value(provider_data.get("modelReasoningEfforts"), model)
        if supported is None:
            supported = provider_data.get("reasoningEfforts")
        if supported is None and metadata:
            supported = metadata.get("reasoningEfforts")
        if model in (provider_data.get("noReasoningModels") or []):
            supported = []
        ceiling = capped_effort(min(caps, key=EFFORTS.index), supported)
        if ceiling is None:
            effective = None
        elif effective in EFFORTS and EFFORTS.index(effective) > EFFORTS.index(ceiling):
            effective = ceiling
    if effective == requested:
        return None
    if protocol == "responses":
        body["reasoning"] = dict(reasoning)
        if effective is None:
            body["reasoning"].pop("effort", None)
        else:
            body["reasoning"]["effort"] = effective
    elif effective is None:
        body.pop("reasoning_effort", None)
    else:
        body["reasoning_effort"] = effective
    return {"from": requested, "to": effective, "subagent": child}
