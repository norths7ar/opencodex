"""One user-requested sizing call, followed by the original deterministic model mapping.

The packaged rubric retains its upstream MIT attribution in role_sizing_prompt.json.
"""

import json
import math
import re
from pathlib import Path

from .subagents import EFFORTS, setting
from .usage_pricing import _bundled_price, _user_rates, pricing_family

TIERS = ["fast", "standard", "frontier"]
INTENTS = ["glance", "measured", "thorough", "exhaustive"]
DOMAINS = {"coding", "agentic", "reasoning", "writing", "knowledge", "general"}
ROLE = "delegated-work"
SYSTEM = json.loads(
    Path(__file__).with_name("role_sizing_prompt.json").read_text(encoding="utf-8")
)["system"]


def classify(candidates, mapping):
    mapped = {}
    for tier in TIERS:
        for model in mapping.get(tier, []):
            mapped.setdefault(model, tier)
    ranked = sorted(
        (row for row in candidates if row["model"] not in mapped and row["unitPrice"] is not None),
        key=lambda row: row["unitPrice"],
    )
    priced = {
        row["model"]: TIERS[
            2 - (len(ranked) - 1 - index)
            if len(ranked) < 3
            else math.floor(index * 2 / (len(ranked) - 1) + 0.5)
        ]
        for index, row in enumerate(ranked)
    }
    return [
        {
            **row,
            "tier": mapped.get(row["model"], priced.get(row["model"])),
            "tierSource": "mapping"
            if row["model"] in mapped
            else "price"
            if row["model"] in priced
            else None,
        }
        for row in candidates
    ]


def candidates(entries, store):
    result = []
    for entry in entries:
        slug = entry["slug"]
        rates = None
        try:
            provider, model = store.route(slug)
            rates, _ = _user_rates(getattr(provider, "modelCosts", {}).get(model))
            family = (
                "openai"
                if provider.authMode == "oauth" and provider.id == "openai"
                else pricing_family(provider)
            )
            if rates is None and family:
                matched = _bundled_price(family, model)
                rates = matched[0] if matched else None
        except ValueError:
            pass
        result.append(
            {
                "model": slug,
                "unitPrice": sum(rates[:2]) if rates else None,
                "efforts": [
                    v["effort"]
                    for v in entry.get("supported_reasoning_levels", [])
                    if v.get("effort") in EFFORTS
                ],
                "defaultEffort": entry.get("default_reasoning_level"),
            }
        )
    return result


def parse_answer(text):
    fenced = re.fullmatch(
        r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```", text.strip(), re.IGNORECASE
    )
    try:
        parsed = json.loads(fenced[1] if fenced else text)
    except ValueError:
        return None, "the sizing model did not answer with JSON"
    roles = parsed.get("roles") if isinstance(parsed, dict) else None
    if not isinstance(roles, dict):
        return None, "the sizing answer has no roles object"
    value = roles.get(ROLE)
    if not isinstance(value, dict):
        return None, "the sizing model did not size this role"
    if value.get("tier") not in TIERS or value.get("effort") not in INTENTS:
        return None, "the sizing answer has no valid tier or effort"
    fields = {}
    for key in ("rationale", "move_up_if", "move_down_if"):
        raw = value.get(key)
        if not isinstance(raw, str):
            return None, "the sizing answer is missing a rationale or a move trigger"
        fields[key] = " ".join(raw.split())
        if not fields[key] or len(fields[key]) > 400:
            return None, "the sizing answer is missing a rationale or a move trigger"
    return {
        **value,
        **fields,
        "domain": value.get("domain") if value.get("domain") in DOMAINS else "general",
    }, None


def proposal(config, answer, classified, *, error=None):
    base = {
        "role": ROLE,
        "model": setting(config, "injectionModel"),
        "effort": setting(config, "injectionEffort"),
    }
    if error:
        return {**base, "status": "unsized", "reason": error}
    sized, error = parse_answer(answer)
    if error:
        return {**base, "status": "unsized", "reason": error}
    choices = [
        row
        for row in classified
        if row["tier"] and TIERS.index(row["tier"]) >= TIERS.index(sized["tier"])
    ]
    choices.sort(
        key=lambda row: (
            TIERS.index(row["tier"]),
            row["unitPrice"] if row["unitPrice"] is not None else math.inf,
            row["model"] != base["model"],
        )
    )
    common = {
        **base,
        "tier": sized["tier"],
        "effortIntent": sized["effort"],
        "domain": sized["domain"],
        "rationale": sized["rationale"],
        "moveUpIf": sized["move_up_if"],
        "moveDownIf": sized["move_down_if"],
    }
    if not choices:
        return {
            **common,
            "status": "unassigned",
            "proposedModel": None,
            "proposedEffort": None,
            "tierSource": None,
            "reason": f"no available model is classified {sized['tier']} or above",
        }
    chosen = choices[0]
    ladder = [level for level in EFFORTS if level in chosen["efforts"]]
    effort = None
    if ladder:
        top = len(ladder) - 1
        default = chosen.get("defaultEffort")
        measured = ladder.index(default) if default in ladder else top // 2
        position = {
            "glance": 0,
            "measured": measured,
            "thorough": min(measured + 1, top),
            "exhaustive": top,
        }[sized["effort"]]
        effort = ladder[position]
    return {
        **common,
        "status": "proposed",
        "proposedModel": chosen["model"],
        "proposedEffort": effort,
        "tierSource": chosen["tierSource"],
        "reason": None,
    }


async def suggest(work, model, config, entries, store, complete):
    classified = classify(candidates(entries, store), setting(config, "codexRoleTiers", {}))
    error = None
    text = ""
    try:
        answer = await complete(
            {
                "model": model,
                "system": SYSTEM,
                "user": "Size these Codex agent roles.\n\n"
                + json.dumps(
                    {"roles": [{"name": ROLE, "instructions": work}]}, ensure_ascii=False, indent=2
                ),
            }
        )
        if answer.get("error"):
            error = "the request could not be completed"
        else:
            text = answer.get("text", "")
    except TimeoutError, OSError, ValueError:
        error = "the request could not be completed"
    return {
        "sizingModel": model,
        "sizingError": error,
        "proposal": proposal(config, text, classified, error=error),
        "candidates": [
            {key: row[key] for key in ("model", "tier", "tierSource", "unitPrice")}
            for row in classified
        ],
    }
