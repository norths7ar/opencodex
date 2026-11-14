"""Anthropic translated Messages contracts ported from adapters/anthropic.ts."""

import copy
import json
import re
from urllib.parse import urlsplit

from .agent_policy import model_value


def strip_encrypted(schema, name_bag=False):
    if isinstance(schema, list):
        return [strip_encrypted(value) for value in schema]
    if not isinstance(schema, dict):
        return schema
    bags = {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependencies",
        "dependentSchemas",
        "dependentRequired",
    }
    literals = {"const", "default", "enum", "examples"}
    return {
        key: copy.deepcopy(value)
        if not name_bag and key in literals
        else strip_encrypted(value, not name_bag and key in bags)
        for key, value in schema.items()
        if name_bag or key != "encrypted"
    }


def input_schema(schema):
    value = strip_encrypted(schema)
    value = value if isinstance(value, dict) else {}
    keys = ("oneOf", "anyOf", "allOf")
    if not any(isinstance(value.get(k), list) for k in keys):
        value["type"] = "object"
        if value.get("properties") is None:
            value["properties"] = {}
        return value
    properties = dict(value.get("properties") or {})
    required = [r for r in value.get("required", []) if isinstance(r, str)]
    for key in keys:
        for branch in value.get(key, []) if isinstance(value.get(key), list) else []:
            if not isinstance(branch, dict):
                continue
            if isinstance(branch.get("properties"), dict):
                properties.update(branch["properties"])
            if key == "allOf":
                required.extend(r for r in branch.get("required", []) if isinstance(r, str))
    result = {k: v for k, v in value.items() if k not in {*keys, "type", "properties", "required"}}
    return {
        **result,
        "type": "object",
        "properties": properties,
        **({"required": list(dict.fromkeys(required))} if required else {}),
    }


def output_schema(schema):
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    value, result = copy.deepcopy(schema), {}
    defs = value.pop("$defs", None)
    if isinstance(defs, dict):
        result["$defs"] = {k: output_schema(v) for k, v in defs.items()}
    if "$ref" in value:
        return {**result, "$ref": value["$ref"]}
    kind = value.pop("type", None)
    composition = next(
        (k for k in ("anyOf", "oneOf", "allOf") if isinstance(value.get(k), list)), None
    )
    if composition:
        result["anyOf" if composition == "oneOf" else composition] = [
            output_schema(v) for v in value.pop(composition)
        ]
    elif kind is None:
        raise ValueError("JSON schema requires a type or anyOf/oneOf/allOf.")
    else:
        result["type"] = kind
    for key in ("description", "title"):
        if key in value:
            result[key] = value.pop(key)
    if kind == "object":
        props = value.pop("properties", {})
        result["properties"] = (
            {k: output_schema(v) for k, v in props.items()} if isinstance(props, dict) else {}
        )
        value.pop("additionalProperties", None)
        result["additionalProperties"] = False
        if "required" in value:
            result["required"] = value.pop("required")
    elif kind == "string" and value.get("format") in {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "uri",
        "ipv4",
        "ipv6",
        "uuid",
    }:
        result["format"] = value.pop("format")
    elif kind == "array":
        if "items" in value:
            result["items"] = output_schema(value.pop("items"))
        if value.get("minItems") in {0, 1}:
            result["minItems"] = value.pop("minItems")
    if value:
        prefix = result.get("description", "")
        result["description"] = (
            (prefix + "\n\n" if isinstance(prefix, str) and prefix else "")
            + "{"
            + ", ".join(
                f"{k}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
                for k, v in value.items()
            )
            + "}"
        )
    return result


def family(model):
    match = re.search(r"(?:^|/)claude-([a-z]+)-(\d+)(?:[.-](\d{1,2}))?(?!\d)", model, re.IGNORECASE)
    return (match[1].lower(), int(match[2]), int(match[3] or 0)) if match else (None, 0, 0)


def native_endpoint(provider):
    url = urlsplit(provider.baseUrl)
    return url.scheme == "https" and url.hostname == "api.anthropic.com" and url.port in {None, 443}


def _policy(provider, model, registry):
    data = provider.model_dump() if hasattr(provider, "model_dump") else dict(provider or {})
    seed = (registry or {}).get("seeds", {}).get(data.get("id"), {})
    if seed.get("baseUrl", "").rstrip("/") != data.get("baseUrl", "").rstrip("/"):
        seed = {}
    merged = {**seed, **data}
    for key in ("modelCapabilities", "modelReasoningEfforts", "modelReasoningEffortMap"):
        merged[key] = {**seed.get(key, {}), **data.get(key, {})}
    caps = {
        **(model_value(seed.get("modelCapabilities"), model) or {}),
        **(model_value(data.get("modelCapabilities"), model) or {}),
    }

    def in_list(values):
        return any(v.lower() == model.lower() for v in values or [])

    if in_list([*seed.get("noReasoningModels", []), *data.get("noReasoningModels", [])]):
        caps.update(
            supportsReasoning=False,
            supportsThinkingToggle=False,
            supportsReasoningEffort=False,
            defaultThinkingEnabled=False,
        )
    efforts = model_value(merged.get("modelReasoningEfforts"), model) or merged.get(
        "reasoningEfforts", []
    )
    mapping = model_value(merged.get("modelReasoningEffortMap"), model) or merged.get(
        "reasoningEffortMap", {}
    )
    strengths = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    if any(mapping.get(e, e) in strengths for e in efforts if e != "none"):
        caps.setdefault("supportsReasoningEffort", True)
    toggle = in_list([*seed.get("thinkingToggleModels", []), *data.get("thinkingToggleModels", [])])
    return merged, caps, mapping, toggle


def apply_anthropic_policy(
    body, source, provider, *, registry=None, cache_retention=None, raw_body=None
):
    """Apply translated options after wire conversion, before OAuth tool-name encoding."""
    model = body["model"]
    cfg, caps, mapping, toggle = _policy(provider, model, registry)
    name, major, minor = family(model)
    version = (major, minor)
    adaptive = (
        name in {"sonnet", "opus", "fable", "haiku"}
        and version >= {"sonnet": (5, 0), "opus": (4, 7), "fable": (0, 0), "haiku": (5, 5)}[name]
    )
    native_thinking = adaptive or name in {"haiku", "sonnet", "opus"}
    configured = model_value(cfg.get("modelMaxOutputTokens"), model)
    if configured is None:
        configured = cfg.get("defaultMaxOutputTokens")
    omitted = configured if isinstance(configured, int | float) and configured > 0 else 8192
    explicit = source.get("max_completion_tokens", source.get("max_tokens"))
    body["max_tokens"] = explicit if explicit is not None else omitted
    requested = source.get("reasoning_effort")
    default = model_value(cfg.get("modelDefaultReasoningEfforts"), model)
    default = default.strip() if isinstance(default, str) else None
    if default in {"", "__omit__"}:
        default = None
    effective = requested if requested is not None else default
    controls_off = caps.get("supportsReasoning") is False or (
        caps.get("supportsThinkingToggle") is False and caps.get("supportsReasoningEffort") is False
    )
    enabled = None
    raw = raw_body or source
    raw_reasoning = raw.get("reasoning") if isinstance(raw.get("reasoning"), dict) else {}
    raw_thinking = raw.get("thinking") if isinstance(raw.get("thinking"), dict) else {}
    if toggle and caps.get("supportsThinkingToggle") is True and not controls_off:
        if type(raw_reasoning.get("enabled")) is bool:
            enabled = raw_reasoning["enabled"]
        elif raw_thinking.get("type") in {"enabled", "disabled"}:
            enabled = raw_thinking["type"] == "enabled"
        else:
            toggle_effort = (
                requested
                if requested is not None
                else default
                if caps.get("supportsReasoningEffort") is False
                else None
            )
            mapped = mapping.get(
                "max" if toggle_effort == "ultra" else toggle_effort, toggle_effort
            )
            if mapped not in {None, "__omit__", "adaptive"}:
                enabled = mapped not in {"none", "disabled"}
            else:
                enabled = caps.get("defaultThinkingEnabled")
    if native_thinking and enabled is not None:
        effective = (
            (effective if effective and effective != "none" else "high") if enabled else "none"
        )
    if not controls_off and enabled is not None and not native_thinking:
        body["thinking"] = {"type": "enabled" if enabled else "disabled"}
    if not controls_off and (
        enabled is None or native_thinking or (enabled and caps.get("supportsReasoningEffort"))
    ):
        if effective == "none" and (
            (name == "sonnet" and (5, 0) <= version < (5, 5))
            or (name == "haiku" and version >= (5, 5))
        ):
            body["thinking"] = {"type": "disabled"}
        elif effective == "none" and name == "sonnet" and version >= (5, 5):
            body["thinking"] = {"type": "between_tools"}
            body.pop("temperature", None)
            body.pop("top_p", None)
        elif (
            enabled is not False
            and (enabled is None or caps.get("supportsReasoningEffort") or native_thinking)
            and isinstance(effective, str)
            and effective != "none"
        ):
            effort = "low" if adaptive and effective == "minimal" else effective
            budget = {
                "minimal": 1024,
                "low": 4096,
                "medium": 8192,
                "high": 16384,
                "xhigh": 24576,
                "max": 32000,
            }.get(effort, 8192)
            if adaptive:
                body["thinking"] = {
                    "type": "adaptive",
                    **(
                        {}
                        if cfg.get("hideThinkingSummary") or raw_reasoning.get("summary") == "none"
                        else {"display": "summarized"}
                    ),
                }
                body.setdefault("output_config", {})["effort"] = effort
                body["max_tokens"] = (
                    explicit
                    if explicit is not None
                    else max(omitted, min(40192, max(8192, budget + 8192)))
                )
            else:
                ceiling = max(configured or 0, 32000)
                maximum = min(
                    ceiling, max(explicit if explicit is not None else omitted, budget + 8192)
                )
                body["max_tokens"] = maximum
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(1024, min(budget, maximum - 4096)),
                }
            body.pop("temperature", None)
            body.pop("top_p", None)
    if (
        native_endpoint(provider)
        and name == "opus"
        and version == (5, 5)
        and not controls_off
        and enabled is not False
    ):
        body["thinking"] = {
            "type": "adaptive",
            "display": "summarized"
            if raw_reasoning.get("summary") in {"concise", "detailed"}
            else "updates",
        }
    if adaptive:
        body.pop("temperature", None)
        body.pop("top_p", None)
    elif name in {"opus", "sonnet", "haiku"} and version >= (4, 5) and "temperature" in body:
        body.pop("top_p", None)
    choice = body.get("tool_choice", {})
    forced_rejected = (
        (name == "opus" and version == (5, 5))
        or (name == "fable" and version >= (5, 1))
        or (name == "sonnet" and version >= (5, 5))
    )
    if forced_rejected and choice.get("type") in {"any", "tool"}:
        if choice["type"] == "tool":
            body["tools"] = [
                t for t in body.get("tools", []) if t.get("name") == choice.get("name")
            ]
        body["tool_choice"] = {"type": "auto"}
    if source.get("parallel_tool_calls") is False and body.get("tool_choice", {}).get(
        "type"
    ) not in {None, "none"}:
        body["tool_choice"]["disable_parallel_tool_use"] = True
    apply_prompt_cache(body, provider, cache_retention)
    return body


def apply_prompt_cache(body, provider, retention=None):
    cc = {
        "type": "ephemeral",
        **(
            {"ttl": "1h"}
            if retention == "long" or (retention is None and native_endpoint(provider))
            else {}
        ),
    }
    automatic = native_endpoint(provider) and retention != "none"
    limit = 3 if automatic else 4
    if automatic:
        body["cache_control"] = dict(cc)
    if isinstance(body.get("system"), str):
        body["system"] = [{"type": "text", "text": body["system"]}]
    messages = body.get("messages", [])
    tools, system = body.get("tools", []), body.get("system", [])
    user = [m for m in messages if m.get("role") == "user"]
    external = any(
        isinstance(m.get("content"), list)
        and any(b.get("cache_control") is not None for b in m["content"] if isinstance(b, dict))
        for m in messages
    )

    def mark_message(message):
        if isinstance(message.get("content"), str):
            message["content"] = [{"type": "text", "text": message["content"]}]
        blocks = message.get("content", [])
        if blocks:
            next((b for b in reversed(blocks) if b.get("type") == "text"), blocks[-1])[
                "cache_control"
            ] = dict(cc)

    if not external and retention != "none":
        used = 0
        if tools:
            tools[-1]["cache_control"] = dict(cc)
            used += 1
        if system and used < limit:
            system[-2 if len(system) > 1 else -1]["cache_control"] = dict(cc)
            used += 1
        if len(user) >= 2 and used < limit:
            mark_message(user[-2])
            used += 1
        if user and not automatic and used < limit:
            mark_message(user[-1])
    message_blocks = [
        b
        for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict)
    ]
    blocks = [*tools, *system, *message_blocks]
    excess = sum(bool(b.get("cache_control")) for b in blocks) - limit
    for block in [*message_blocks, *system, *tools]:
        if excess > 0 and block.pop("cache_control", None):
            excess -= 1
    short = False
    for block in blocks:
        control = block.get("cache_control")
        if not isinstance(control, dict):
            continue
        if control.get("ttl") != "1h":
            short = True
        elif short:
            control.pop("ttl", None)
