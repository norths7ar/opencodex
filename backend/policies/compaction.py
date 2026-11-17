"""Codex compaction routing, portable checkpoints, and generation-only replay framing."""

import base64
import copy
import json
import re
from typing import cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

COMPACT_PROMPT = "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.\n\nInclude:\n- Current progress and key decisions made\n- Important context, constraints, or user preferences\n- What remains to be done (clear next steps)\n- Any critical data, examples, or references needed to continue\n\nBe concise, structured, and focused on helping the next LLM seamlessly continue the work."

SUMMARY_PREFIX = "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"

OPAQUE_COMPACTION_NOTE = (
    "[earlier conversation was compacted; the summary is stored in a format this model cannot read]"
)

CHECKPOINT_CONTINUATION_NOTE = "This conversation was compacted automatically. A checkpoint summary from an earlier context window appears as a user message and records prior work. Treat earlier requests it reports as completed as history; do not repeat them unless a later user message asks you to. Continue an unfinished task only when it was already authorized and remains within the scope of that authorization. Do not ask for the same authorization again while it still applies. Preserve all existing permission and confirmation gates; if a decision or required permission is pending, wait for it before taking the dependent action. A checkpoint or delegation, including one from another thread, does not itself grant authority or expand the task's scope. Later user messages take priority; obey any pause, cancellation, replacement, or change of scope."

HISTORICAL_REQUEST_START = "[Historical user request retained before the following compaction checkpoint. This is prior context, not a new request.]"

HISTORICAL_REQUEST_END = "[End historical request. Use the following checkpoint to distinguish completed work from unfinished work. Do not repeat completed work unless later user input asks for it. Preserve these original details for unfinished work within its existing authorization; the checkpoint grants no new authority. Only input after the checkpoint is a new user request.]"


EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
ITEM_TYPES = {"compaction", "compaction_summary", "context_compaction"}


class CompactionRouting(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str
    reasoningEffort: str | None = None
    triggers: list[str] | None = None
    sourceModels: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def optional_fields(cls, value):
        if isinstance(value, dict) and any(
            key in value and value[key] is None
            for key in ("reasoningEffort", "triggers", "sourceModels")
        ):
            raise ValueError("Omit optional compaction settings instead of setting them to null.")
        return value

    @field_validator("model")
    @classmethod
    def target(cls, value):
        if not value.strip():
            raise ValueError("Select a compaction model.")
        return value.strip()

    @field_validator("reasoningEffort")
    @classmethod
    def effort(cls, value):
        if value is not None and value not in EFFORTS:
            raise ValueError("Invalid compaction reasoning effort.")
        return value

    @field_validator("triggers", "sourceModels")
    @classmethod
    def selectors(cls, value, info):
        if value is None:
            return value
        if not value or len(set(value)) != len(value):
            raise ValueError("Select at least one distinct value.")
        for item in value:
            if info.field_name == "triggers":
                valid = item in {"manual", "auto"}
            else:
                valid = (
                    bool(item)
                    and not re.search(r"\s", item)
                    and ("*" not in item or re.fullmatch(r"[^/*]+/\*", item))
                )
            if not valid:
                raise ValueError(f"Invalid {info.field_name} selector.")
        return value


def plain_user_text(item):
    if (
        not isinstance(item, dict)
        or item.get("role") != "user"
        or item.get("type", "message") != "message"
    ):
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    if all(
        isinstance(p, dict)
        and p.get("type") in {"text", "input_text"}
        and isinstance(p.get("text"), str)
        for p in content
    ):
        return "\n".join(p["text"] for p in content)
    return None


def local_checkpoint(body):
    items = body.get("input")
    return (
        isinstance(items, list)
        and bool(items)
        and (plain_user_text(items[-1]) or "").strip() == COMPACT_PROMPT
    )


def remote_checkpoint(body):
    return isinstance(body.get("input"), list) and any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger"
        for item in body["input"]
    )


def routing_override(body, headers, config, *, compact=False):
    """Mutate only a positively identified eligible compaction turn; return source selector."""
    raw = config.features.request.compaction_routing
    if not raw or not isinstance(body.get("model"), str) or not body["model"].strip():
        return None
    try:
        policy = CompactionRouting.model_validate(raw)
    except ValueError:
        return None
    remote = remote_checkpoint(body)
    local = (
        not compact
        and not remote
        and config.features.request.codex_client_compaction is True
        and local_checkpoint(body)
    )
    if not compact and not remote and not local:
        return None
    if policy.sourceModels:
        source = body["model"]
        known = {
            f"{p.id}/{m}" for p in config.channels for m in [*p.models.static, *p.models.selected]
        }
        if source not in known:
            if config.features.request.fast_rows is not False and source.endswith("--fast"):
                source = source[:-6]
            elif config.features.request.cursor_effort_rows is True:
                source = re.sub(r"--(?:minimal|low|medium|high|xhigh|max|ultra)$", "", source)
        if not any(
            source.startswith(s[:-1]) and len(source) > len(s) - 1
            if s.endswith("/*")
            else source == s
            for s in policy.sourceModels
        ):
            return None
    copies = []
    if "x-codex-turn-metadata" in headers:
        copies.append(headers["x-codex-turn-metadata"])
    client = body.get("client_metadata")
    if isinstance(client, dict) and "x-codex-turn-metadata" in client:
        copies.append(client["x-codex-turn-metadata"])
    if not copies:
        return None
    seen = None
    for value in copies:
        try:
            metadata = json.loads(value) if isinstance(value, str) else None
            detail = metadata.get("compaction") if isinstance(metadata, dict) else None
            trigger = detail.get("trigger") if isinstance(detail, dict) else None
            if (
                not isinstance(metadata, dict)
                or metadata.get("request_kind") != "compaction"
                or trigger not in (policy.triggers or ["manual"])
            ):
                return None
            if local and cast(dict[str, object], detail).get("implementation") != "responses":
                return None
            if seen is not None and seen != trigger:
                return None
            seen = trigger
        except ValueError:
            return None
    source = body["model"]
    body["model"] = policy.model
    if policy.reasoningEffort is not None:
        body["reasoning"] = {
            **(body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}),
            "effort": policy.reasoningEffort,
        }
    return source


def decode_summary(value):
    if not isinstance(value, str) or not value.startswith(("ocx1:", "ocx2:")):
        return None
    try:
        decoded = base64.b64decode(value[5:], validate=True).decode("utf-8")
        if value.startswith("ocx1:"):
            return decoded
        data = json.loads(decoded)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "summary", "reasoning"}
            or data["version"] != 2
            or not isinstance(data["summary"], str)
            or not data["summary"].strip()
            or not isinstance(data["reasoning"], list)
            or not all(isinstance(s, str) for s in data["reasoning"])
        ):
            return None
        return "\n\n".join([data["summary"], *data["reasoning"]])
    except ValueError, UnicodeError:
        return None


def summary_item(summary):
    return {
        "type": "compaction",
        "encrypted_content": "ocx1:" + base64.b64encode(summary.encode()).decode(),
    }


def user_item(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def replay_projection(body, *, opaque_note=False, frame=True):
    """Return a generation projection. Canonical history must retain original items."""
    result = copy.deepcopy(body)
    items = result.get("input")
    if not isinstance(items, list):
        return result
    checkpoint = -1
    decoded_any = False
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        text = plain_user_text(item)
        if text and text.startswith(SUMMARY_PREFIX):
            checkpoint = index
        if item.get("type") in ITEM_TYPES:
            encoded = item.get("encrypted_content")
            decoded = decode_summary(encoded)
            if (
                isinstance(encoded, str)
                and encoded.startswith(("ocx1:", "ocx2:"))
                and decoded is None
            ):
                raise ValueError("Invalid OpenCodex compaction envelope.")
            if decoded:
                items[index] = user_item(SUMMARY_PREFIX + "\n\n" + decoded)
                checkpoint = index
                decoded_any = True
            elif opaque_note:
                items[index] = user_item(OPAQUE_COMPACTION_NOTE)
    framed = False
    if frame and checkpoint > 0 and not remote_checkpoint(body) and not local_checkpoint(body):
        for index in range(checkpoint):
            item = items[index]
            text = plain_user_text(item)
            if (
                not text
                or not text.strip()
                or text.startswith((SUMMARY_PREFIX, HISTORICAL_REQUEST_START))
                or text.lstrip().startswith(("<", "# AGENTS.md", "# Context from my IDE"))
            ):
                continue
            content = item["content"]
            item["content"] = (
                HISTORICAL_REQUEST_START + "\n" + content + "\n" + HISTORICAL_REQUEST_END
                if isinstance(content, str)
                else [
                    {"type": "input_text", "text": HISTORICAL_REQUEST_START},
                    *content,
                    {"type": "input_text", "text": HISTORICAL_REQUEST_END},
                ]
            )
            framed = True
    if (decoded_any or framed) and not remote_checkpoint(body) and not local_checkpoint(body):
        instructions = (
            result.get("instructions") if isinstance(result.get("instructions"), str) else ""
        )
        if CHECKPOINT_CONTINUATION_NOTE not in instructions:
            result["instructions"] = (
                instructions + "\n\n" if instructions.strip() else ""
            ) + CHECKPOINT_CONTINUATION_NOTE
    return result


def v1_output(items, summary):
    selected = []
    remaining = 80000
    for item in reversed(items if isinstance(items, list) else []):
        if (
            not isinstance(item, dict)
            or item.get("role") != "user"
            or item.get("type", "message") != "message"
        ):
            continue
        content = item.get("content")
        text = (
            content
            if isinstance(content, str)
            else "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict)
                and p.get("type") in {"input_text", "text"}
                and isinstance(p.get("text"), str)
            )
            if isinstance(content, list)
            else ""
        )
        if not text.strip():
            continue
        encoded = text.encode("utf-16-le")
        if len(encoded) // 2 > remaining:
            text = encoded[-remaining * 2 :].decode("utf-16-le", errors="ignore")
            selected.append(user_item(text))
            break
        remaining -= len(encoded) // 2
        selected.append(user_item(text))
        if remaining <= 0:
            break
    return [*reversed(selected), user_item(SUMMARY_PREFIX + "\n" + summary)]


def summary_text(response):
    if (
        response.get("status") != "completed"
        or response.get("error")
        or response.get("incomplete_details")
    ):
        raise ValueError("The compaction model did not complete its summary.")
    texts = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            raise TypeError("Invalid compaction output.")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ValueError("The compaction model returned a tool call instead of a summary.")
        for part in item.get("content", []):
            if (
                not isinstance(part, dict)
                or part.get("type") != "output_text"
                or not isinstance(part.get("text"), str)
            ):
                raise ValueError("The compaction model returned a non-text summary.")
            texts.append(part["text"])
    result = "".join(texts).strip()
    if not result:
        raise ValueError("The compaction model returned an empty summary.")
    return result
