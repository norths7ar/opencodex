"""Responses request codec."""

import json
from typing import cast

from .agent_messages import routed_agent_messages
from .anthropic_reasoning import blocks as thinking_blocks
from .anthropic_reasoning import decode as thinking_decode
from .anthropic_reasoning import from_item as thinking_from_item
from .anthropic_reasoning import output_items as thinking_items
from .anthropic_reasoning import progress_mirror
from .content import chat_content, text_content
from .tools import identifier, response_tools


def responses_to_chat(body, *, preserve_tool_media=False):
    original_items = body.get("input")
    body = routed_agent_messages(body, include_identities=False)
    tools, tool_names = response_tools(body)
    search_name = next(
        (name for name, identity in tool_names.items() if identity["search"]), "tool_search"
    )
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    items = body.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    reasoning = ""
    reasoning_details = []
    for item_index, item in enumerate(items):
        if item_index and progress_mirror(item, items[item_index - 1]):
            continue
        kind = item.get("type", "message")
        if kind == "web_search_call":
            # Without a sidecar, original run-turn-execution strips replayed search
            # pairs again. Do not invent a callable tool or fake search-result text.
            reasoning = ""
            continue
        if kind in {"additional_tools", "configuration_update"}:
            continue
        if kind == "tool_search_call":
            value = item.get("arguments", {})
            item = {
                **item,
                "name": search_name,
                "arguments": value if isinstance(value, str) else json.dumps(value),
            }
            kind = "function_call"
        elif kind == "tool_search_output":
            item = {
                **item,
                "output": json.dumps(
                    {
                        "tools": item.get("tools", []),
                        **({"status": item["status"]} if "status" in item else {}),
                    }
                ),
            }
            kind = "function_call_output"
        if kind == "message" and item.get("role", "user") != "assistant" and reasoning_details:
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_details": reasoning_details,
                    **({"reasoning_content": reasoning} if reasoning else {}),
                }
            )
            reasoning_details, reasoning = [], ""
        if kind == "message":
            role = item.get("role", "user")
            if (
                isinstance(original_items, list)
                and original_items[item_index].get("type") == "agent_message"
            ):
                # This is an external user turn, not a continuation of the assistant's thought.
                reasoning = ""
            messages.append({"role": role, "content": chat_content(item.get("content", ""))})
            if role == "assistant" and reasoning:
                messages[-1]["reasoning_content"] = reasoning
                reasoning = ""
            if role == "assistant" and reasoning_details:
                messages[-1]["reasoning_details"] = reasoning_details
                reasoning_details = []
        elif kind in {"function_call", "custom_tool_call"}:
            namespace = item.get("namespace")
            if not namespace and kind == "custom_tool_call":
                matches = {
                    v.get("namespace")
                    for v in tool_names.values()
                    if v["name"] == item["name"] and v.get("custom")
                }
                if len(matches) == 1:
                    namespace = matches.pop()
            if namespace == "functions":
                namespace = None
            wire_name = f"{namespace}__{item['name']}" if namespace else item["name"]
            call = {
                "id": item["call_id"],
                "type": "function",
                "function": {
                    "name": wire_name,
                    "arguments": json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
                    if kind == "custom_tool_call"
                    else item.get("arguments", ""),
                },
            }
            if messages and messages[-1]["role"] == "assistant":
                message = messages[-1]
            else:
                message = {"role": "assistant", "content": None}
                messages.append(message)
            message.setdefault("tool_calls", []).append(call)
            if reasoning:
                message["reasoning_content"] = reasoning
                reasoning = ""
            if reasoning_details:
                message["reasoning_details"] = reasoning_details
                reasoning_details = []
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output", "")
            parts = chat_content(output)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": parts if preserve_tool_media else text_content(parts),
                }
            )
            if isinstance(parts, list) and not preserve_tool_media:
                media = [p for p in parts if p.get("type") != "text"]
                if media:
                    messages.append({"role": "user", "content": media})
        elif kind == "reasoning":
            details = thinking_from_item(item)
            carrier = thinking_decode(item.get("encrypted_content")) or {}
            if carrier.get("aft"):
                prior = items[item_index - 1] if item_index else {}
                proper = (
                    prior.get("type") in {"function_call", "custom_tool_call"}
                    or (
                        prior.get("type") == "tool_search_call"
                        and prior.get("execution") == "client"
                    )
                    or (
                        prior.get("type", "message") == "message"
                        and prior.get("role") == "assistant"
                    )
                )
                if (
                    details
                    and proper
                    and messages
                    and messages[-1].get("role") == "assistant"
                    and prior.get("id", carrier["aft"]) == carrier["aft"]
                ):
                    messages[-1].setdefault("reasoning_details", []).extend(details)
                continue
            reasoning_details.extend(details)
            if item.get("encrypted_content") and not item.get("summary") and not details:
                raise ValueError(
                    "Opaque reasoning cannot be replayed to a different wire protocol."
                )
            summary = cast(list[dict[str, str]], item.get("summary", []))
            reasoning += text_content(summary) or "".join(p.get("text", "") for p in summary)
        else:
            raise ValueError(f"The translated route cannot represent Responses item {kind!r}.")
    if reasoning_details:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_details": reasoning_details,
                **({"reasoning_content": reasoning} if reasoning else {}),
            }
        )
    result = {"model": body["model"], "messages": messages, "stream": bool(body.get("stream"))}
    for key in ("temperature", "top_p", "parallel_tool_calls", "seed", "user", "metadata"):
        if key in body:
            result[key] = body[key]
    if "max_output_tokens" in body:
        result["max_tokens"] = body["max_output_tokens"]
    if body.get("reasoning", {}).get("effort"):
        result["reasoning_effort"] = body["reasoning"]["effort"]
    if body.get("text", {}).get("format"):
        format_ = body["text"]["format"]
        result["response_format"] = (
            {
                "type": "json_schema",
                "json_schema": {k: v for k, v in format_.items() if k != "type"},
            }
            if format_.get("type") == "json_schema"
            else format_
        )
    if tools:
        chat_tools = []
        for tool in tools:
            if tool.get("type") in {"web_search", "web_search_preview"}:
                # parser-tools drops hosted search; only an active sidecar may
                # inject the synthetic executable function. Lite has no sidecar.
                continue
            if tool.get("type") == "custom":
                chat_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "input": {
                                        "type": "string",
                                        "description": "Raw tool input. For apply_patch, begin exactly with `*** Begin Patch` (no trailing `***`), then use its standard patch envelope."
                                        if tool_names[tool["name"]]["name"] == "apply_patch"
                                        else "Raw freeform input for this tool.",
                                    }
                                },
                                "required": ["input"],
                                "additionalProperties": False,
                            },
                        },
                    }
                )
                continue
            if tool.get("type") != "function" and not tool.get("name"):
                raise ValueError(
                    f"The translated route cannot execute built-in tool {tool.get('type')!r}."
                )
            function = {
                k: v
                for k, v in tool.items()
                if k in {"name", "description", "parameters", "strict"}
            }
            function["parameters"] = {**(function.get("parameters") or {}), "type": "object"}
            chat_tools.append({"type": "function", "function": function})
        if chat_tools:
            result["tools"] = chat_tools
    choice = body.get("tool_choice")
    required = choice == "required" or (
        isinstance(choice, dict)
        and choice.get("type") == "allowed_tools"
        and choice.get("mode") == "required"
    )
    if isinstance(choice, dict) and choice.get("type") in {"web_search", "web_search_preview"}:
        # mapToolChoice does not force hosted search; sidecar selection is separate.
        choice = "auto"
    if isinstance(choice, dict) and choice.get("type") == "tool_search":
        choice = {"type": "function", "name": search_name}
    if isinstance(choice, dict) and choice.get("type") == "allowed_tools":
        names = {
            search_name if tool.get("type") == "tool_search" else tool.get("name")
            for tool in choice.get("tools", [])
        }
        result["tools"] = [
            tool for tool in result.get("tools", []) if tool["function"]["name"] in names
        ]
        choice = choice.get("mode", "auto") if result["tools"] else "none"
    if not result.get("tools"):
        if required:
            raise ValueError(
                "The requested tool choice requires an executable tool; hosted web search has no configured sidecar on this translated route."
            )
        result.pop("tools", None)
        choice = None
    if choice is not None:
        result["tool_choice"] = (
            {
                "type": "function",
                "function": {
                    "name": f"{choice['namespace']}__{choice['name']}"
                    if choice.get("namespace") and choice["namespace"] != "functions"
                    else choice["name"]
                },
            }
            if isinstance(choice, dict) and choice.get("type") in {"function", "custom"}
            else choice
        )
    if result["stream"]:
        result["stream_options"] = {"include_usage": True}
    return result


def chat_to_responses(body):
    items = []
    for message in body.get("messages", []):
        role = message["role"]
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message.get("content") or "",
                }
            )
            continue
        if thinking_blocks(message.get("reasoning_details")):
            items.extend(
                item
                for entry in message["reasoning_details"]
                for item in thinking_items(entry, identifier("rs"))
            )
        elif message.get("reasoning_content"):
            items.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": message["reasoning_content"]}],
                }
            )
        if message.get("content") is not None:
            content = message["content"]
            if isinstance(content, list):
                converted = []
                for part in content:
                    if part["type"] == "text":
                        converted.append(
                            {
                                "type": "output_text" if role == "assistant" else "input_text",
                                "text": part["text"],
                            }
                        )
                    elif part["type"] == "image_url":
                        converted.append(
                            {
                                "type": "input_image",
                                "image_url": part["image_url"]["url"],
                                **(
                                    {"detail": part["image_url"]["detail"]}
                                    if "detail" in part["image_url"]
                                    else {}
                                ),
                            }
                        )
                    else:
                        raise ValueError(
                            f"Cannot translate Chat content {part['type']!r} to Responses."
                        )
                content = converted
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls", []):
            items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    result = {"model": body["model"], "input": items, "stream": bool(body.get("stream"))}
    for key in ("temperature", "top_p", "parallel_tool_calls", "user", "metadata"):
        if key in body:
            result[key] = body[key]
    maximum = body.get("max_completion_tokens", body.get("max_tokens"))
    if maximum is not None:
        result["max_output_tokens"] = maximum
    if body.get("reasoning_effort"):
        result["reasoning"] = {"effort": body["reasoning_effort"]}
    if "tools" in body:
        result["tools"] = [{"type": "function", **t["function"]} for t in body["tools"]]
    if "tool_choice" in body:
        choice = body["tool_choice"]
        result["tool_choice"] = (
            {"type": "function", **choice["function"]} if isinstance(choice, dict) else choice
        )
    if body.get("response_format"):
        fmt = body["response_format"]
        result["text"] = {
            "format": {"type": "json_schema", **fmt["json_schema"]}
            if fmt.get("type") == "json_schema"
            else fmt
        }
    if body.get("n", 1) != 1:
        raise ValueError("Responses translation supports one completion per request.")
    return result
