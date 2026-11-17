"""Anthropic Messages request codec."""

import copy
import json

from .anthropic_history import repair_messages
from .anthropic_reasoning import blocks as thinking_blocks
from .anthropic_reasoning import detail as thinking_detail
from .content import chat_content, text_content
from .schema import input_schema, output_schema


def messages_to_chat(body):
    messages = []
    if body.get("system"):
        messages.append({"role": "system", "content": chat_content(body["system"])})
    for message in body.get("messages", []):
        content = message.get("content", [])
        if isinstance(content, str):
            messages.append({"role": message["role"], "content": content})
            continue
        parts, calls, results, thinking, details = [], [], [], "", []
        for part in content:
            kind = part["type"]
            if kind == "tool_use":
                calls.append(
                    {
                        "id": part["id"],
                        "type": "function",
                        "function": {
                            "name": part["name"],
                            "arguments": json.dumps(part["input"], ensure_ascii=False),
                        },
                    }
                )
            elif kind == "tool_result":
                result = chat_content(part.get("content", ""))
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": part["tool_use_id"],
                        "content": text_content(result),
                    }
                )
                if isinstance(result, list):
                    parts.extend(p for p in result if p["type"] != "text")
            elif kind == "thinking":
                thinking += part["thinking"]
                details.append(thinking_detail(part, len(details)))
            elif kind == "redacted_thinking":
                details.append(thinking_detail(part, len(details)))
            else:
                parts.extend(chat_content([part]))
        messages.extend(results)
        if parts or calls or thinking or details:
            output = {"role": message["role"], "content": parts or None}
            if calls:
                output["tool_calls"] = calls
            if thinking:
                output["reasoning_content"] = thinking
            if details:
                output["reasoning_details"] = details
            messages.append(output)
    result = {"model": body["model"], "messages": messages, "stream": bool(body.get("stream"))}
    for key in ("max_tokens", "temperature", "top_p", "metadata"):
        if key in body:
            result[key] = body[key]
    if "stop_sequences" in body:
        result["stop"] = body["stop_sequences"]
    if "tools" in body:
        result["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            }
            for t in body["tools"]
        ]
    if "tool_choice" in body:
        choice = body["tool_choice"]
        result["tool_choice"] = (
            {"type": "function", "function": {"name": choice["name"]}}
            if choice["type"] == "tool"
            else {"any": "required", "auto": "auto", "none": "none"}[choice["type"]]
        )
        if choice.get("disable_parallel_tool_use"):
            result["parallel_tool_calls"] = False
    return result


def anthropic_content(content):
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    result = []
    for part in parts:
        if part["type"] == "text":
            if part.get("text"):
                result.append(copy.deepcopy(part))
        elif part["type"] == "image_url":
            raw = part["image_url"]
            url = raw if isinstance(raw, str) else raw["url"]
            if url.startswith("data:"):
                meta, data = url[5:].split(",", 1)
                result.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": meta.split(";")[0],
                            "data": data,
                        },
                    }
                )
            else:
                result.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            raise ValueError(f"Cannot translate Chat content {part['type']!r} to Messages.")
    return result


def chat_to_messages(body):
    result = {
        "model": body["model"],
        "messages": [],
        "max_tokens": body.get("max_completion_tokens", body.get("max_tokens", 8192)),
        "stream": bool(body.get("stream")),
    }
    systems = []
    for message in body.get("messages", []):
        role = message["role"]
        if role in {"system", "developer"}:
            systems.append(text_content(message.get("content")))
            continue
        if role == "tool":
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": anthropic_content(message.get("content")) or "(empty tool output)",
                    **({"is_error": True} if message.get("is_error") else {}),
                }
            ]
            role = "user"
        else:
            content = thinking_blocks(message.get("reasoning_details")) + anthropic_content(
                message.get("content")
            )
            for call in message.get("tool_calls", []):
                content.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": json.loads(call["function"].get("arguments") or "{}"),
                    }
                )
        if content:
            result["messages"].append({"role": role, "content": content})
    result["messages"] = repair_messages(result["messages"])
    if systems:
        result["system"] = [{"type": "text", "text": text} for text in systems if text]
    for key in ("temperature", "top_p"):
        if key in body:
            result[key] = body[key]
    if body.get("stop"):
        result["stop_sequences"] = [body["stop"]] if isinstance(body["stop"], str) else body["stop"]
    if "tools" in body:
        result["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": input_schema(t["function"].get("parameters", {"type": "object"})),
            }
            for t in body["tools"]
        ]
    if body.get("tool_choice"):
        choice = body["tool_choice"]
        tool_choice: dict[str, str | bool] = (
            {"type": "tool", "name": choice["function"]["name"]}
            if isinstance(choice, dict)
            else {"type": {"required": "any", "auto": "auto", "none": "none"}[choice]}
        )
        result["tool_choice"] = tool_choice
        if body.get("parallel_tool_calls") is False and tool_choice["type"] != "none":
            tool_choice["disable_parallel_tool_use"] = True
    elif result.get("tools") and body.get("parallel_tool_calls") is False:
        result["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    fmt = body.get("response_format", {})
    if fmt.get("type") == "json_schema" and fmt.get("json_schema", {}).get("schema"):
        result["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema(fmt["json_schema"]["schema"])}
        }
    return result
