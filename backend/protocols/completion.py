"""Non-streaming response translation between Chat and upstream protocols."""

import copy
import json
import time
from typing import NotRequired, TypedDict, cast

from .anthropic_reasoning import blocks as thinking_blocks
from .anthropic_reasoning import chat_usage as anthropic_chat_usage
from .anthropic_reasoning import detail as thinking_detail
from .anthropic_reasoning import output_items as thinking_items
from .content import text_content
from .tools import identifier, response_tool_identity


class _AssistantMessage(TypedDict):
    role: str
    content: str
    tool_calls: NotRequired[list[dict[str, object]]]
    reasoning_content: NotRequired[str]
    reasoning_details: NotRequired[list[object]]


def response_to_chat(data, protocol, *, progress_updates=False, hide_thinking=False):
    if protocol == "chat/completions":
        return data
    message: _AssistantMessage = {"role": "assistant", "content": ""}
    tools, reasoning, reasoning_details = [], "", []
    if protocol == "responses":
        for item in data.get("output", []):
            if item["type"] == "message":
                message["content"] += text_content(item.get("content"))
            elif item["type"] == "function_call":
                tools.append(
                    {
                        "id": item["call_id"],
                        "type": "function",
                        "function": {"name": item["name"], "arguments": item["arguments"]},
                    }
                )
            elif item["type"] == "reasoning":
                reasoning += "".join(p.get("text", "") for p in item.get("summary", []))
            else:
                raise ValueError(f"Cannot translate Responses output item {item['type']!r}.")
        usage = data.get("usage") or {}
        finish = (
            "length" if data.get("status") == "incomplete" else "tool_calls" if tools else "stop"
        )
    else:
        for part in data.get("content", []):
            if part["type"] == "text":
                message["content"] += part["text"]
            elif part["type"] == "thinking":
                reasoning += part["thinking"]
                reasoning_details.append(
                    {
                        **cast(dict[str, object], thinking_detail(part, len(reasoning_details))),
                        **({"_progress": True} if progress_updates else {}),
                        **({"_hidden": True} if hide_thinking else {}),
                    }
                )
            elif part["type"] == "redacted_thinking":
                reasoning_details.append(thinking_detail(part, len(reasoning_details)))
            elif part["type"] == "tool_use":
                tools.append(
                    {
                        "id": part["id"],
                        "type": "function",
                        "function": {
                            "name": part["name"],
                            "arguments": json.dumps(part["input"], ensure_ascii=False),
                        },
                    }
                )
            else:
                raise ValueError(f"Cannot translate Messages output block {part['type']!r}.")
        if data.get("stop_reason") == "error":
            raise ValueError("Upstream reported a failed Messages completion.")
        usage = data.get("usage") or {}
        finish = {
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "refusal": "content_filter",
            "content_filter": "content_filter",
        }.get(data.get("stop_reason"), "stop")
    if tools:
        message["tool_calls"] = tools
    if reasoning:
        message["reasoning_content"] = reasoning
    if reasoning_details:
        message["reasoning_details"] = reasoning_details
    incoming, outgoing = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    converted = (
        anthropic_chat_usage(usage)
        if protocol == "messages"
        else {
            "prompt_tokens": incoming,
            "completion_tokens": outgoing,
            "total_tokens": incoming + outgoing,
        }
    )
    return {
        "id": data.get("id", identifier("chatcmpl")),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model", ""),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": converted,
        **(
            {"_terminal_refusal": True}
            if protocol == "messages" and data.get("stop_reason") in {"refusal", "content_filter"}
            else {}
        ),
    }


def response_from_chat(data, protocol, response_id=None, custom_tools=(), tool_names=None):
    if protocol == "chat/completions":
        return {key: value for key, value in data.items() if not key.startswith("_")}
    choice = data["choices"][0]
    message = choice["message"]
    usage = data.get("usage") or {}
    converted_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }
    if usage.get("prompt_tokens_details"):
        converted_usage["input_tokens_details"] = copy.deepcopy(usage["prompt_tokens_details"])
    if protocol == "responses":
        output = []
        if message.get("reasoning_details"):
            output.extend(
                item
                for entry in message["reasoning_details"]
                for item in thinking_items(entry, identifier("rs"))
            )
        elif message.get("reasoning_content"):
            output.append(
                {
                    "id": identifier("rs"),
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": message["reasoning_content"]}],
                }
            )
        if message.get("content"):
            output.append(
                {
                    "id": identifier("msg"),
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text_content(message["content"]),
                            "annotations": [],
                        }
                    ],
                }
            )
        for call in message.get("tool_calls", []):
            if (tool_names or {}).get(call["function"]["name"], {}).get("search"):
                output.append(
                    {
                        "id": identifier("tsc"),
                        "type": "tool_search_call",
                        "status": "completed",
                        "execution": "client",
                        "call_id": call["id"],
                        "arguments": json.loads(call["function"].get("arguments") or "{}"),
                    }
                )
                continue
            if call["function"]["name"] in custom_tools:
                value = json.loads(call["function"].get("arguments") or "{}").get("input")
                if not isinstance(value, str):
                    raise ValueError("A custom tool call must contain a string input.")
                output.append(
                    {
                        "id": identifier("ct"),
                        "type": "custom_tool_call",
                        "status": "completed",
                        "call_id": call["id"],
                        **response_tool_identity(call["function"]["name"], tool_names),
                        "input": value,
                    }
                )
                continue
            output.append(
                {
                    "id": identifier("fc"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call["id"],
                    **call["function"],
                    **response_tool_identity(call["function"]["name"], tool_names),
                }
            )
        converted_usage["total_tokens"] = (
            converted_usage["input_tokens"] + converted_usage["output_tokens"]
        )
        converted_usage["input_tokens_details"] = {
            "cached_tokens": 0,
            **copy.deepcopy(usage.get("prompt_tokens_details", {})),
        }
        converted_usage["output_tokens_details"] = {
            "reasoning_tokens": usage.get("completion_tokens_details", {}).get(
                "reasoning_tokens", 0
            )
        }
        reason = {
            "length": "max_output_tokens",
            "content_filter": "content_filter",
            "adapter_eof": "adapter_eof",
            None: "adapter_eof",
        }.get(choice.get("finish_reason"))
        incomplete = reason is not None
        if incomplete:
            for item in output:
                if item["type"] in {"function_call", "custom_tool_call", "tool_search_call"}:
                    item["status"] = "incomplete"
        refusal = bool(data.get("_terminal_refusal"))
        refusal_error = {
            "type": "invalid_request_error",
            "code": "invalid_prompt",
            "message": "The upstream model refused this request. Retrying with the same conversation context will not succeed; change the request or start a new conversation.",
        }
        return {
            "id": response_id or identifier("resp"),
            "object": "response",
            "created_at": data.get("created", int(time.time())),
            "status": "failed" if refusal else "incomplete" if incomplete else "completed",
            "error": refusal_error if refusal else None,
            **({"last_error": refusal_error, "retryable": False} if refusal else {}),
            "incomplete_details": {"reason": reason} if incomplete else None,
            "model": data.get("model", ""),
            "output": output,
            "usage": converted_usage,
            "parallel_tool_calls": True,
        }
    content = thinking_blocks(message.get("reasoning_details"))
    if not content and message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": text_content(message["content"])})
    for call in message.get("tool_calls", []):
        content.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["function"]["name"],
                "input": json.loads(call["function"].get("arguments") or "{}"),
            }
        )
    return {
        "id": response_id or identifier("msg"),
        "type": "message",
        "role": "assistant",
        "model": data.get("model", ""),
        "content": content,
        "stop_reason": {"length": "max_tokens", "tool_calls": "tool_use"}.get(
            choice.get("finish_reason"), "end_turn"
        ),
        "stop_sequence": None,
        "usage": converted_usage,
    }
