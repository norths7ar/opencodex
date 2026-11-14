"""Codex roster projection and opt-in delegation, ported from catalog/collaboration."""

import copy
import json
import re

from . import clients
from .subagent_surface import collaboration_surface

EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
MODE_HINT = {
    "revision": "proactive-trigger-v1",
    "text": "Proactive multi-agent delegation is active. "
    "Only the delegation trigger changes: a separate explicit request is no longer required. "
    "All existing user, authority, task-scope, and collaboration-tool rules continue to apply. "
    "Delegate eligible independent work when parallel execution could materially improve speed or quality. "
    "User requests override this hint. "
    "This mode remains active until a later multi-agent mode developer message changes it.",
}
_TAG = "opencodex_subagent_guidance"


def setting(config, key, default=None):
    return config.get(key, default) if isinstance(config, dict) else getattr(config, key, default)


def feature_state(home):
    document = clients._parse(clients._read(home / "config.toml", 1 << 20, "Codex config.toml"))
    values = document.unwrap()
    feature = values.get("features", {}).get("multi_agent_v2", False)
    feature = {"enabled": feature} if isinstance(feature, bool) else feature
    agents = values.get("agents", {})
    return {
        "enabled": feature.get("enabled", bool(feature)),
        "maxConcurrentThreadsPerSession": feature.get(
            "max_concurrent_threads_per_session", agents.get("max_threads")
        ),
        "agentsEnabled": agents.get("enabled"),
        "agentsMaxDepth": agents.get("max_depth"),
        "subagentDeveloperInstructions": feature.get("subagent_developer_instructions"),
        "multiAgentModeHintText": feature.get("multi_agent_mode_hint_text"),
    }


def project_catalog(entries, config, *, v2_enabled=False):
    """Priority controls Codex's five-model advertisement; source rows remain read-only."""
    result = copy.deepcopy(entries)
    featured = setting(config, "subagentModels", [])
    picker = setting(config, "modelPickerOrder", [])
    mode = setting(config, "multiAgentMode", "default")
    for index, entry in enumerate(result):
        slug = entry["slug"]
        if featured:
            entry["priority"] = featured.index(slug) if slug in featured else 100 + index
        if mode in {"v1", "v2"}:
            native = slug.startswith("openai/") or bool(re.match(r"^(?:gpt-|o[134](?:-|$))", slug))
            entry["multi_agent_version"] = (
                "v1"
                if mode == "v2" and native and setting(config, "keepNativeChatGptOnV1", False)
                else mode
            )
        elif v2_enabled and "multi_agent_version" not in entry:
            entry["multi_agent_version"] = "v2"
        if picker:
            entry["opencodex_spawn_priority"] = entry.get("priority", index)
            entry["priority"] = 1000 + (
                picker.index(slug) if slug in picker else len(picker) + index
            )
    return result


def effective_roster(entries, configured, surface="v2"):
    candidates = sorted(
        (
            entry
            for entry in entries
            if entry.get("visibility") == "list"
            and (surface != "v2" or entry.get("multi_agent_version") != "disabled")
        ),
        key=lambda entry: entry.get("opencodex_spawn_priority", entry.get("priority", 2**31)),
    )[:5]
    advertised = [entry for entry in candidates if entry["slug"] in configured]
    return candidates, advertised


def collab_surface(body):
    return collaboration_surface(body)


def guidance_text(body, config, entries, *, catalog_state="unknown"):
    if setting(config, "multiAgentGuidanceEnabled", True) is False:
        return None
    surface = collab_surface(body)
    if surface is None:
        return None
    if surface == "v1":
        effort = body.get("reasoning", {}).get("effort") or body.get("reasoning_effort")
        return (
            f"<multi_agent_mode>{MODE_HINT['text']}</multi_agent_mode>"
            if effort in {"max", "ultra"}
            else None
        )
    if catalog_state in {"unknown", "stale"}:
        return None
    chosen = setting(config, "subagentModels", [])
    preferred = setting(config, "injectionModel")
    candidates, roster = effective_roster(entries, chosen)
    preferred = preferred if preferred in {entry["slug"] for entry in candidates} else None
    fallback = setting(config, "subagentModelFallback", [])
    if not preferred and not roster and not fallback:
        return None
    effort = setting(config, "injectionEffort")
    roster_text = ""
    if roster:
        descriptions = []
        for entry in roster:
            levels = "/".join(
                level["effort"] for level in entry.get("supported_reasoning_levels", [])
            )
            descriptions.append(f'"{entry["slug"]}"' + (f" ({levels})" if levels else ""))
        roster_text = " Available models (valid reasoning_effort): " + ", ".join(descriptions) + "."
    fallback_text = (
        " Configured fallback order: " + ", ".join(json.dumps(model) for model in fallback) + "."
        if fallback
        else ""
    )
    custom = setting(config, "injectionPrompt")
    if custom:
        text = custom
        for key, value in {
            "model": preferred or "",
            "effort": effort or "",
            "roster": roster_text,
            "fallback": fallback_text,
        }.items():
            text = text.replace("{{" + key + "}}", value)
    else:
        text = "OpenCodex sub-agent routing metadata for this collaboration surface. "
        text += "This metadata does not override Codex delegation or model-selection rules."
        if preferred:
            text += f' Preferred sub-agent: model "{preferred}"'
            text += (f', reasoning_effort "{effort}"' if effort else "") + "."
        text += fallback_text
        if len(text + roster_text) <= 700:
            text += roster_text
    return f"<{_TAG}>{text}</{_TAG}>"


def apply_guidance(body, config, entries, *, catalog_state="unknown"):
    """Mutate a Responses body before protocol conversion, preserving tool-result ordering."""
    text = guidance_text(body, config, entries, catalog_state=catalog_state)
    if text is None:
        return False
    raw = body.get("input", [])
    items = [{"role": "user", "content": raw}] if isinstance(raw, str) else list(raw)
    if any(
        isinstance(item, dict)
        and item.get("role") == "developer"
        and item.get("content") == [{"type": "input_text", "text": text}]
        for item in items
    ):
        return False
    index = 0
    if body.get("previous_response_id"):
        index = next(
            (
                i
                for i, item in enumerate(items)
                if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
            ),
            len(items),
        )
        if index == len(items) and items and items[-1].get("type") == "compaction_trigger":
            index -= 1
    else:
        while (
            index < len(items)
            and isinstance(items[index], dict)
            and items[index].get("role") in {"system", "developer"}
        ):
            index += 1
    items.insert(
        index,
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]},
    )
    body["input"] = items
    return True
