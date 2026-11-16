"""Policies for request bodies translated onto the Messages wire."""

import re
from urllib.parse import urlsplit

from backend.configuration.models import ChannelRequestPolicy


def direct_messages_endpoint(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.anthropic.com"
        and parsed.port in {None, 443}
    )


def model_in(model: str, values: list[str] | None) -> bool:
    return any(value.lower() == model.lower() for value in values or [])


def model_value(values, model):
    if not isinstance(values, dict):
        return None
    if model in values:
        return values[model]
    family, separator, _ = model.partition(":")
    if separator and family in values:
        return values[family]
    return next((value for key, value in values.items() if key.lower() == model.lower()), None)


def family(model):
    match = re.search(r"(?:^|/)claude-([a-z]+)-(\d+)(?:[.-](\d{1,2}))?(?!\d)", model, re.IGNORECASE)
    return (match[1].lower(), int(match[2]), int(match[3] or 0)) if match else (None, 0, 0)


def apply_anthropic_policy(
    body: dict,
    source: dict,
    policy: ChannelRequestPolicy,
    metadata: dict,
    *,
    direct: bool,
    cache_retention: str | None,
    raw_body: dict,
):
    """Apply options that are resolved only after converting to the Messages wire."""
    model = body["model"]
    caps = dict(metadata)
    mapping = model_value(policy.modelReasoningEffortMap, model) or policy.reasoningEffortMap or {}
    if model_in(model, policy.noReasoningModels):
        caps.update(
            supportsReasoning=False,
            supportsThinkingToggle=False,
            supportsReasoningEffort=False,
            defaultThinkingEnabled=False,
        )
    strengths = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    if any(mapping.get(e, e) in strengths for e in caps.get("reasoningEfforts", []) if e != "none"):
        caps.setdefault("supportsReasoningEffort", True)
    toggle = model_in(model, policy.thinkingToggleModels)
    name, major, minor = family(model)
    version = (major, minor)
    adaptive = (
        name in {"sonnet", "opus", "fable", "haiku"}
        and version >= {"sonnet": (5, 0), "opus": (4, 7), "fable": (0, 0), "haiku": (5, 5)}[name]
    )
    native_thinking = adaptive or name in {"haiku", "sonnet", "opus"}
    configured = metadata.get("maxOutputTokens", policy.defaultMaxOutputTokens)
    omitted = configured if isinstance(configured, int | float) and configured > 0 else 8192
    explicit = source.get("max_completion_tokens", source.get("max_tokens"))
    body["max_tokens"] = explicit if explicit is not None else omitted
    requested = source.get("reasoning_effort")
    default = metadata.get("defaultReasoningEffort")
    default = default.strip() if isinstance(default, str) else None
    if default in {"", "__omit__"}:
        default = None
    effective = requested if requested is not None else default
    controls_off = caps.get("supportsReasoning") is False or (
        caps.get("supportsThinkingToggle") is False and caps.get("supportsReasoningEffort") is False
    )
    enabled = None
    raw = raw_body
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
                        if policy.hideThinkingSummary or raw_reasoning.get("summary") == "none"
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
        direct
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
    apply_prompt_cache(body, direct=direct, retention=cache_retention)
    return body


def apply_prompt_cache(body: dict, *, direct: bool, retention: str | None = None):
    cc = {
        "type": "ephemeral",
        **({"ttl": "1h"} if retention == "long" or (retention is None and direct) else {}),
    }
    automatic = direct and retention != "none"
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
