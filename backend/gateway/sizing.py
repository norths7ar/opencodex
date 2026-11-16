"""User-requested role sizing via the ordinary internal generation executor."""

import asyncio
import json
import math
import re
from pathlib import Path
from uuid import uuid4

import httpx

from backend.policies.effort import EFFORTS
from backend.telemetry.pricing import _bundled_price, _user_rates, pricing_family

from .context import RequestContext, RequestPurpose

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
        "model": config.features.agents.injection_model,
        "effort": config.features.agents.injection_effort,
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


class SizingRecommendations:
    def __init__(self, dispatcher, catalog, config_loader):
        self.dispatcher, self.catalog, self.config_loader = dispatcher, catalog, config_loader

    def candidates(self):
        rows = []
        for entry in self.catalog():
            if not entry.visible:
                continue
            channel = next(
                (c for c in self.config_loader().channels if c.id == entry.identity.channel_id),
                None,
            )
            if channel is None:
                continue
            model = entry.identity.native_id
            rates, _ = _user_rates(channel.models.metadata.costs.get(model))
            family = (
                "openai"
                if channel.service_id == "chatgpt" and channel.auth_mode == "oauth"
                else pricing_family(channel)
            )
            if rates is None and family:
                match = _bundled_price(family, model)
                rates = match[0] if match else None
            meta = entry.metadata.values
            rows.append(
                {
                    "model": entry.routed_selector,
                    "unitPrice": sum(rates[:2]) if rates else None,
                    "efforts": [v for v in (meta.get("reasoningEfforts") or []) if v in EFFORTS],
                    "defaultEffort": meta.get("defaultReasoningEffort"),
                }
            )
        return rows

    async def __call__(self, work, model):
        config = self.config_loader()
        classified = classify(self.candidates(), config.features.agents.codex_role_tiers)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": "Size these Codex agent roles.\n\n"
                    + json.dumps(
                        {"roles": [{"name": ROLE, "instructions": work}]},
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],
            "stream": False,
        }
        cancellation = asyncio.Event()
        context = RequestContext(
            str(uuid4()),
            "chat/completions",
            model,
            False,
            cancellation,
            purpose=RequestPurpose.SIZING,
        )
        error = None
        text = ""
        try:
            async with self.dispatcher.execute(context, body, {}) as response:
                if (
                    response.status >= 400
                    or response.content is None
                    or len(response.content) > 1 << 20
                ):
                    error = "the request could not be completed"
                else:
                    payload = json.loads(response.content)
                    text = payload["choices"][0]["message"]["content"]
                    if not isinstance(text, str) or not text.strip():
                        error = "the request could not be completed"
        except TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, httpx.HTTPError:
            error = "the request could not be completed"
        finally:
            cancellation.set()
        return {
            "sizingModel": model,
            "sizingError": error,
            "proposal": proposal(config, text, classified, error=error),
            "candidates": [
                {key: row[key] for key in ("model", "tier", "tierSource", "unitPrice")}
                for row in classified
            ],
        }
