"""Model selection rules ported from providers/{default-aliases,model-presets,new-model-policy}."""

import re
from datetime import UTC, datetime
from uuid import uuid4

DEFAULT_CONTEXT_CAP = 350_000
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ALIAS_RULES = [
    (r"^claude-opus-5", "opus"),
    (r"^claude-sonnet-5", "sonnet"),
    (r"^claude-haiku", "haiku"),
    (r"^gemini-3(?:\.\d+)?-pro", "g3p"),
    (r"^gemini-3(?:\.\d+)?-flash", "g3f"),
    (r"^deepseek-v4\.1", "ds41"),
    (r"^deepseek-v4", "ds4"),
    (r"^deepseek-flash", "dsf"),
    (r"^grok-4", "grok"),
]
ANTHROPIC_PRESET = [
    r"^claude-opus-[45]",
    r"^claude-sonnet-[45]",
    r"^claude-haiku-[45]",
    r"^claude-fable-5",
]
PRESETS = {
    "openrouter": [
        r"^anthropic/claude-(opus|sonnet|haiku)-[45]",
        r"^google/gemini-3(\.\d+)?-(pro|flash)",
        r"^openai/gpt-5(\.\d+)?",
        r"^deepseek/deepseek-(v4|r2)",
        r"^x-ai/grok-4(\.\d+)?",
        r"^moonshotai/kimi-k[23]",
        r"^z-ai/glm-[45]",
    ],
    "anthropic": ANTHROPIC_PRESET,
    "anthropic-apikey": ANTHROPIC_PRESET,
}


def materialize_preset(name, ids):
    return list(
        dict.fromkeys(
            model
            for model in ids
            if any(re.search(pattern, model) for pattern in PRESETS.get(name, []))
        )
    )


def effective_aliases(config, provider, known_ids):
    known = set(known_ids)
    result = {
        model: {"alias": alias, "source": "user"}
        for model, alias in provider.modelAliases.items()
        if ALIAS_PATTERN.fullmatch(alias)
    }
    if not getattr(provider, "defaultAliases", config.defaultModelAliases):
        return result
    claims = {}
    for model in known:
        if model in result:
            continue
        alias = next(
            (
                alias
                for pattern, alias in ALIAS_RULES
                if re.search(pattern, model) or re.search(pattern, model.rsplit("/", 1)[-1])
            ),
            None,
        )
        if alias:
            claims.setdefault(alias, []).append(model)
    occupied = {v["alias"].lower() for v in result.values()} | {m.lower() for m in known}
    for alias, models in claims.items():
        if len(models) == 1 and alias.lower() not in occupied:
            result[models[0]] = {"alias": alias, "source": "builtin"}
    return result


def resolve_model_alias(config, provider, known_ids, requested):
    return next(
        (
            model
            for model, row in effective_aliases(config, provider, known_ids).items()
            if row["alias"].lower() == requested.lower()
        ),
        None,
    )


def initialize_selection(provider):
    provider.initialModelSelection = {
        "version": 1,
        "registrationId": str(uuid4()),
        "status": "pending",
    }


def mark_preset_diverged(provider):
    marker = getattr(provider, "modelPreset", None)
    if marker and marker.get("mode") == "preset":
        provider.modelPreset = {**marker, "mode": "custom"}


def reconcile_discovery(config, provider, ids, *, converge=False):
    """Replace discovery history from a complete roster, preserving operator selections."""
    discovered = sorted(set(ids))
    before = config.model_dump()
    initial = getattr(provider, "initialModelSelection", None)
    if initial and initial.get("status") == "pending":
        all_off = len(discovered) >= 20
        if all_off:
            config.disabledModels = list(
                dict.fromkeys(
                    [
                        *config.disabledModels,
                        *[f"{provider.id}/{m.replace('/', '-')}" for m in discovered],
                    ]
                )
            )
        provider.initialModelSelection = {
            **initial,
            "status": "all-off" if all_off else "ready",
            "modelCount": len(discovered),
        }
    if not provider.liveModels:
        return config.model_dump() != before
    discovery = dict(getattr(config, "modelDiscovery", {}))
    known = dict(discovery.get("knownModels", {}))
    prior = known.get(provider.id)
    now = datetime.now(UTC).isoformat()
    if prior is None:
        known[provider.id] = {"ids": discovered, "removed": [], "updatedAt": now}
    else:
        active, removed, seen = set(prior["ids"]), set(prior.get("removed", [])), set(discovered)
        arrivals = sorted(seen - active)
        removed = (removed | (active - seen)) - seen
        next_value = {
            "ids": discovered,
            "removed": sorted(removed),
        }
        comparable = {key: value for key, value in prior.items() if key != "updatedAt"}
        known[provider.id] = {
            **next_value,
            "updatedAt": prior["updatedAt"] if comparable == next_value else now,
        }
        policy = getattr(provider, "newModelPolicy", None)
        if policy not in {"on", "off"}:
            policy = discovery.get("newModelPolicy", "on")
        if policy == "off" and not provider.selectedModels:
            config.disabledModels = list(
                dict.fromkeys(
                    [
                        *config.disabledModels,
                        *[f"{provider.id}/{m.replace('/', '-')}" for m in arrivals],
                    ]
                )
            )
        if arrivals:
            recent = dict(discovery.get("recentArrivals", {}))
            recent[provider.id] = [
                *recent.get(provider.id, []),
                *[{"id": m, "at": now} for m in arrivals],
            ][-50:]
            discovery["recentArrivals"] = recent
    discovery["knownModels"] = known
    config.modelDiscovery = discovery
    return config.model_dump() != before
