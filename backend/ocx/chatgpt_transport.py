"""Canonical subscription request policy, separate from public Responses API transport."""

import copy
import hashlib
import json
from pathlib import Path

from .oauth import CHATGPT_BASE
from .subscription_tools import normalize_subscription_tools

FORWARD_HEADERS = (
    "openai-beta",
    "originator",
    "session_id",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-parent-thread-id",
    "x-codex-turn-state",
    "x-codex-window-id",
    "x-openai-subagent",
    "x-responsesapi-include-timing-metrics",
    "version",
    "x-openai-internal-codex-responses-lite",
)


def subscription_headers(provider, credential, incoming=None):
    headers = {k.lower(): v for k, v in provider.headers.items()}
    for field in FORWARD_HEADERS:
        if incoming and incoming.get(field):
            headers[field] = incoming[field]
    headers.update(
        {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "authorization": f"Bearer {credential['access']}",
            "chatgpt-account-id": credential["accountId"],
            "x-codex-installation-id": credential["installationId"],
        }
    )
    headers.pop("x-oai-attestation", None)
    headers.setdefault("originator", "codex_cli_rs")
    return headers


def subscription_body(body, *, compact=False, credential=None):
    value = copy.deepcopy(body)
    normalize_subscription_tools(value, compact=compact)
    for field in ("previous_response_id", "conversation", "background", "prompt", "metadata"):
        value.pop(field, None)
    if compact:
        value.pop("stream", None)
        value.pop("store", None)
        return value
    value.update(store=False, stream=True)
    value.pop("prompt_cache_options", None)
    model = value.get("model", "")
    if model == "gpt-5.6" or model.startswith("gpt-5.6-"):
        value.pop("prompt_cache_retention", None)
    value.setdefault("instructions", "")
    for field in (
        "max_output_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "user",
        "safety_identifier",
        "truncation",
    ):
        value.pop(field, None)
    if isinstance(value.get("input"), str):
        value["input"] = [{"role": "user", "content": value["input"]}]
    value["input"] = [
        item
        for item in value.get("input", [])
        if not isinstance(item, dict) or item.get("type") != "item_reference"
    ]
    pending = list(value["input"])
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            item.pop("prompt_cache_breakpoint", None)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    occupied = {
        item["call_id"]
        for item in value["input"]
        if isinstance(item, dict)
        and isinstance(item.get("call_id"), str)
        and len(item["call_id"]) <= 64
    }
    aliases = {}
    for item in value["input"]:
        original = item.get("call_id") if isinstance(item, dict) else None
        if not isinstance(original, str) or len(original) <= 64:
            continue
        if original not in aliases:
            salt = 0
            while True:
                source = original if salt == 0 else f"{original}\0{salt}"
                alias = "call_ocx_" + hashlib.sha256(source.encode()).hexdigest()[:55]
                if alias not in occupied:
                    break
                salt += 1
            aliases[original] = alias
            occupied.add(alias)
        item["call_id"] = aliases[original]
    system = [
        item
        for item in value.get("input", [])
        if isinstance(item, dict)
        and item.get("type", "message") == "message"
        and item.get("role") == "system"
    ]
    texts = []
    for item in system:
        content = item.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list) and all(
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text"}
            and isinstance(block.get("text"), str)
            for block in content
        ):
            texts.append("".join(block["text"] for block in content))
        else:
            break
    if system and len(texts) == len(system):
        value["input"] = [item for item in value["input"] if item not in system]
        value["instructions"] = "\n\n".join([value["instructions"], *texts]).strip()
    for item in value.get("input", []):
        if isinstance(item, dict):
            item.pop("id", None)
            item.pop("extra_content", None)
    metadata = value.get("client_metadata")
    if credential and isinstance(metadata, dict):
        if "x-codex-installation-id" in metadata:
            metadata["x-codex-installation-id"] = credential["installationId"]
        metadata.pop("x-oai-attestation", None)
    return value


def discovery_version(provider):
    explicit = getattr(provider, "modelDiscoveryClientVersion", None)
    if explicit:
        return explicit
    # Use the shipped protocol baseline, not a native client's login or files.
    template = json.loads(
        Path(__file__).with_name("codex_catalog_template.json").read_text(encoding="utf-8")
    )
    rows = (
        template.get("models", [template.get("entry", {})])
        if isinstance(template, dict)
        else template
    )
    versions = ["0.159.0"]
    versions.extend(
        row["minimal_client_version"]
        for row in rows
        if isinstance(row.get("minimal_client_version"), str)
    )
    return max(
        versions, key=lambda v: tuple(int(p) for p in v.split("-")[0].split(".") if p.isdigit())
    )


def subscription_url(protocol):
    return f"{CHATGPT_BASE}/{protocol}"
