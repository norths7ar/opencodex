"""Original ocxr1 and OpenRouter-style Anthropic signed-thinking carriers."""

import base64
import copy
import json
import re

FORMAT = "anthropic-claude-v1"


def signature(value):
    return (
        isinstance(value, str)
        and len(value) >= 16
        and not re.match(
            r"^(fc|call|msg|rs|resp|reasoning|item|ws|tool|func|function)[-_]", value, re.IGNORECASE
        )
        and re.fullmatch(r"[A-Za-z0-9+/_=-]+", value) is not None
    )


def envelope(value):
    return (
        "ocxr1:"
        + base64.b64encode(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode()
    )


def decode(value):
    if not isinstance(value, str) or not value.startswith("ocxr1:"):
        return None
    try:
        result = json.loads(base64.b64decode(value[6:], validate=True))
        return result if isinstance(result, dict) else None
    except ValueError, UnicodeError:
        return None


def detail(block, index=0):
    if block.get("type") == "redacted_thinking" and isinstance(block.get("data"), str):
        return {
            "type": "reasoning.encrypted",
            "data": block["data"],
            "format": FORMAT,
            "index": index,
        }
    if block.get("type") == "thinking":
        return {
            "type": "reasoning.text",
            "text": block.get("thinking", ""),
            **({"signature": block["signature"]} if signature(block.get("signature")) else {}),
            "format": FORMAT,
            "index": index,
        }
    return None


def blocks(details):
    """Accept the complete signed carrier; malformed/foreign details remain plaintext."""
    if not isinstance(details, list) or not details:
        return []
    result = []
    for entry in details:
        if not isinstance(entry, dict):
            return []
        if (
            entry.get("type") == "reasoning.encrypted"
            and entry.get("format") == FORMAT
            and isinstance(entry.get("data"), str)
            and entry["data"]
        ):
            result.append({"type": "redacted_thinking", "data": entry["data"]})
        elif (
            entry.get("type") == "reasoning.text"
            and entry.get("format") in {None, FORMAT}
            and isinstance(entry.get("text"), str)
            and signature(entry.get("signature"))
        ):
            result.append(
                {"type": "thinking", "thinking": entry["text"], "signature": entry["signature"]}
            )
        else:
            return []
    return result


def from_item(item):
    value = decode(item.get("encrypted_content"))
    if value is None:
        return []
    result = [
        detail({"type": "redacted_thinking", "data": d})
        for d in value.get("red", [])
        if isinstance(d, str)
    ]
    text = value.get("txt", "".join(p.get("text", "") for p in item.get("summary", [])))
    if signature(value.get("sig")):
        result.append(detail({"type": "thinking", "thinking": text, "signature": value["sig"]}))
    return result


def to_item(entry, identity):
    text = entry.get("text", "") if entry.get("type") == "reasoning.text" else ""
    result = {
        "id": identity,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}]
        if text and not entry.get("_hidden")
        else [],
    }
    value = (
        {"red": [entry["data"]]}
        if entry.get("type") == "reasoning.encrypted"
        else {"sig": entry["signature"]}
        if signature(entry.get("signature"))
        else {}
    )
    if entry.get("_hidden") and text:
        value["txt"] = text
    if value:
        result["encrypted_content"] = envelope(value)
    return result


def update_details(message, incoming):
    rows = message.setdefault("reasoning_details", [])
    for value in incoming:
        found = next((r for r in rows if r.get("index") == value.get("index")), None)
        if found is None:
            rows.append(copy.deepcopy(value))
        else:
            found.update(copy.deepcopy(value))


def chat_usage(usage):
    base, output = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    read, write = (
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0),
    )
    incoming = base + read + write
    result = {
        "prompt_tokens": incoming,
        "completion_tokens": output,
        "total_tokens": incoming + output,
    }
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        result["prompt_tokens_details"] = {
            "cached_tokens": read,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
        }
        split = usage.get("cache_creation", {})
        if isinstance(split, dict) and "ephemeral_1h_input_tokens" in split:
            result["prompt_tokens_details"]["cache_creation_1h_input_tokens"] = min(
                write, split["ephemeral_1h_input_tokens"]
            )
    return result


def progress_mirror(item, previous):
    value = decode(previous.get("encrypted_content")) if isinstance(previous, dict) else None
    if not value or not re.fullmatch(r"msg_[a-f0-9]{32}", str(value.get("upd", ""))):
        return False
    parts = item.get("content")
    return (
        isinstance(value.get("txt"), str)
        and item.get("type", "message") == "message"
        and item.get("role") == "assistant"
        and item.get("phase") == "commentary"
        and item.get("id", value["upd"]) == value["upd"]
        and isinstance(parts, list)
        and len(parts) == 1
        and parts[0].get("type") == "output_text"
        and parts[0].get("text") == value["txt"]
    )


def output_items(entry, identity):
    if not entry.get("_progress") or not entry.get("text") or not signature(entry.get("signature")):
        return [to_item(entry, identity)]
    import uuid

    message_id = "msg_" + uuid.uuid4().hex
    return [
        {
            "type": "reasoning",
            "id": identity,
            "summary": [],
            "encrypted_content": envelope(
                {"sig": entry["signature"], "txt": entry["text"], "upd": message_id}
            ),
        },
        {
            "type": "message",
            "id": message_id,
            "role": "assistant",
            "status": "completed",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": entry["text"], "annotations": []}],
        },
    ]
