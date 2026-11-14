"""Kimi coding wire policy ported from registry entries and their adapter callers."""

import copy
import json
from pathlib import Path

SEED = json.loads(Path(__file__).with_name("kimi_registry.json").read_text(encoding="utf-8"))[
    "seeds"
]["kimi"]
RANKS = ["low", "medium", "high", "xhigh", "max", "ultra"]


def effective_provider(provider):
    """Fill registry defaults only; saved operator policy remains authoritative."""
    if provider.id not in {"kimi", "kimi-responses"}:
        return provider
    saved = provider.model_dump(exclude_none=True)
    updates = {}
    for key, value in SEED.items():
        if key in {"adapter", "baseUrl", "authMode"}:
            continue
        if isinstance(value, dict) and isinstance(saved.get(key), dict):
            folded = {model.lower() for model in saved[key]}
            updates[key] = {
                **{
                    model: copy.deepcopy(item)
                    for model, item in value.items()
                    if model.lower() not in folded
                },
                **saved[key],
            }
        elif key not in saved:
            updates[key] = copy.deepcopy(value)
    return provider.model_copy(update=updates)


def model_in(values, model):
    return model in (values or []) or (":" in model and model.split(":", 1)[0] in (values or []))


def record_value(values, model):
    values = values or {}
    if model in values:
        return values[model]
    if ":" in model and model.split(":", 1)[0] in values:
        return values[model.split(":", 1)[0]]
    return next((value for key, value in values.items() if key.lower() == model.lower()), None)


def strip_suffix(model):
    end = len(model.rstrip())
    if not end or model[end - 1] != "]":
        return model
    start = -1
    for index in range(end - 2, -1, -1):
        if model[index] == "]":
            break
        if model[index] == "[":
            start = index
    return model if start < 0 else model[:start]


def map_effort(provider, model, requested):
    if (
        not isinstance(requested, str)
        or not requested
        or model_in(getattr(provider, "noReasoningModels", []), model)
    ):
        return None
    boundary = "max" if requested == "ultra" else requested
    maps = record_value(getattr(provider, "modelReasoningEffortMap", {}), model)
    if maps is None:
        maps = getattr(provider, "reasoningEffortMap", {})
    maps = maps or {}
    if boundary in maps:
        return None if maps[boundary] == "__omit__" else maps[boundary]
    effort = "low" if boundary == "minimal" else boundary
    if effort not in RANKS:
        return None
    ladder = record_value(getattr(provider, "modelReasoningEfforts", {}), model)
    if ladder is None:
        ladder = getattr(provider, "reasoningEfforts", None)
    if ladder is not None:
        # Mapped Codex aliases are part of the effective catalog ladder in the source.
        ladder = sorted(
            set(ladder)
            | {key for key, value in maps.items() if key in RANKS and value != "__omit__"},
            key=lambda x: RANKS.index(x) if x in RANKS else -1,
        )
        if not ladder:
            return None
        if effort not in ladder:
            below = [
                value
                for value in ladder
                if value in RANKS and RANKS.index(value) <= RANKS.index(effort)
            ]
            effort = below[-1] if below else ladder[0]
    effort = "max" if effort == "ultra" else effort
    result = maps.get(effort, effort)
    return None if result == "__omit__" else result


def _positions(items):
    calls, outputs = {}, {}
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("call_id"), str)
            or not item["call_id"]
        ):
            continue
        kind = item.get("type")
        if kind in {"function_call", "local_shell_call", "custom_tool_call"}:
            target = calls
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            target = outputs
        else:
            continue
        key = ("custom" if kind.startswith("custom") else "function", item["call_id"])
        target.setdefault(key, []).append(index)
    return calls, outputs


def adjacent_results(items):
    """Merge readable repeated outputs, then move only globally unambiguous batches."""
    calls, outputs = _positions(items)
    merged, dropped = {}, set()
    for key, indexes in outputs.items():
        positions = calls.get(key, [])
        if len(indexes) < 2 or len(positions) != 1 or indexes[0] < positions[0]:
            continue
        pieces = [items[index].get("output") for index in indexes]
        if not all(isinstance(piece, (str, list)) for piece in pieces):
            continue
        if all(isinstance(piece, str) for piece in pieces):
            joined = "\n\n".join(piece for piece in pieces if piece)
        else:
            joined = []
            for piece in pieces:
                joined.extend(
                    piece
                    if isinstance(piece, list)
                    else [{"type": "input_text", "text": piece}]
                    if piece
                    else []
                )
        merged[indexes[0]] = {**items[indexes[0]], "output": joined}
        dropped.update(indexes[1:])
    items = [merged.get(index, item) for index, item in enumerate(items) if index not in dropped]
    calls, outputs = _positions(items)
    pairs = []
    for key in calls.keys() | outputs.keys():
        a, b = calls.get(key, []), outputs.get(key, [])
        if len(a) != 1 or len(b) != 1 or b[0] <= a[0]:
            return items
        pairs.append((a[0], b[0]))
    pairs.sort()
    batches, moved = {}, set()
    cursor = 0
    while cursor < len(pairs):
        group = [pairs[cursor]]
        first_output = pairs[cursor][1]
        cursor += 1
        while cursor < len(pairs) and pairs[cursor][0] < first_output:
            group.append(pairs[cursor])
            first_output = min(first_output, pairs[cursor][1])
            cursor += 1
        if any(group[i][1] < group[i - 1][1] for i in range(1, len(group))):
            return items
        indexes = [pair[0] for pair in group] + [pair[1] for pair in group]
        anchor = group[0][0]
        if indexes != list(range(anchor, anchor + len(indexes))):
            batches[anchor] = [items[index] for index in indexes]
            moved.update(indexes)
    result = []
    for index, item in enumerate(items):
        result.extend(batches.get(index, []))
        if index not in moved:
            result.append(item)
    return result


def prepare_kimi(body, provider, model, protocol, *, translated=False, raw_body=None):
    """Apply after protocol conversion, before serialization and physical-send estimation.

    Native Chat keeps its caller effort except ultra, as the source passthrough does.
    Responses encrypted reasoning items remain opaque and are never dropped here.
    """
    provider = effective_provider(provider)
    result = copy.deepcopy(body)
    if provider.id not in {"kimi", "kimi-responses"}:
        return result
    if getattr(provider, "modelSuffixBracketStrip", False):
        result["model"] = strip_suffix(result.get("model", model))
    if protocol == "responses":
        reasoning = result.get("reasoning")
        if isinstance(reasoning, dict):
            ladder = record_value(getattr(provider, "modelReasoningEfforts", {}), model)
            if ladder == []:
                reasoning.pop("effort", None)
                if not reasoning:
                    result.pop("reasoning", None)
            elif ladder is not None or getattr(provider, "reasoningEfforts", None) is not None:
                mapped = map_effort(provider, model, reasoning.get("effort"))
                if mapped:
                    reasoning["effort"] = mapped
        if getattr(provider, "requiresAdjacentResponsesToolResults", False) and isinstance(
            result.get("input"), list
        ):
            result["input"] = adjacent_results(result["input"])
        return result
    if protocol != "chat/completions":
        return result
    for flag, fields in (
        ("noTemperatureModels", ["temperature"]),
        ("noTopPModels", ["top_p"]),
        ("noPenaltyModels", ["presence_penalty", "frequency_penalty"]),
    ):
        if model_in(getattr(provider, flag, []), model):
            for field in fields:
                result.pop(field, None)
    if (
        translated
        and result.get("tools")
        and "tool_choice" in result
        and model_in(getattr(provider, "autoToolChoiceOnlyModels", []), model)
        and result["tool_choice"] != "none"
    ):
        result["tool_choice"] = "auto"
    effort = result.get("reasoning_effort")
    if translated or effort == "ultra":
        mapped = map_effort(provider, model, effort)
        if mapped is None:
            result.pop("reasoning_effort", None)
        else:
            result["reasoning_effort"] = mapped
    if getattr(provider, "promptCacheKey", False) and raw_body and "prompt_cache_key" in raw_body:
        result["prompt_cache_key"] = raw_body["prompt_cache_key"]
    return result
