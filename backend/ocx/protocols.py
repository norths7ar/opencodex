"""Wire codecs for translated requests; native routes do not pass through these codecs.

The migration reference is backend/src/protocols/codecs, adapters/openai-chat and bridge.
Unsupported opaque/built-in items fail explicitly instead of disappearing from history.
"""

import copy
import json
import time
import uuid

from .anthropic_history import repair_messages
from .anthropic_policy import input_schema, output_schema
from .anthropic_reasoning import blocks as thinking_blocks
from .anthropic_reasoning import chat_usage as anthropic_chat_usage
from .anthropic_reasoning import decode as thinking_decode
from .anthropic_reasoning import detail as thinking_detail
from .anthropic_reasoning import from_item as thinking_from_item
from .anthropic_reasoning import output_items as thinking_items
from .anthropic_reasoning import progress_mirror


def identifier(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def response_tools(body):
    """Flatten the original Responses tool identities, retaining their return addresses."""
    flattened, identities = [], {}
    declarations = list(body.get("tools", []))
    if isinstance(body.get("input"), list):
        for item in body["input"]:
            if item.get("type") in {"additional_tools", "tool_search_output"}:
                declarations.extend(item.get("tools", []))
    declared_names = {
        (child.get("function") or child).get("name")
        for spec in declarations
        for child in (spec.get("tools", []) if spec.get("type") == "namespace" else [spec])
        if child.get("type") != "tool_search"
    }
    search_name = "tool_search"
    if search_name in declared_names:
        search_name = "opencodex_tool_search"
        suffix = 2
        while search_name in declared_names:
            search_name = f"opencodex_tool_search_{suffix}"
            suffix += 1
    for spec in declarations:
        namespace = spec.get("name") if spec.get("type") == "namespace" else None
        namespace = None if namespace == "functions" else namespace
        children = spec.get("tools", []) if spec.get("type") == "namespace" else [spec]
        for child in children:
            tool = copy.deepcopy(child)
            if tool.get("type") == "tool_search":
                tool = {
                    "type": "function",
                    "name": search_name,
                    "description": tool.get(
                        "description", "Search for additional tools to load for the next turn."
                    ),
                    "parameters": tool.get(
                        "parameters",
                        {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Search query for tools to load.",
                                },
                                "limit": {
                                    "type": "number",
                                    "description": "Maximum number of tools to return.",
                                },
                            },
                            "required": ["query"],
                        },
                    ),
                    "_tool_search": True,
                }
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                tool = {"type": "function", **tool["function"]}
            if not isinstance(tool.get("name"), str):
                flattened.append(tool)
                continue
            name = tool["name"]
            wire = f"{namespace}__{name}" if namespace else name
            identity = {
                "name": name,
                **({"namespace": namespace} if namespace else {}),
                "custom": tool.get("type") == "custom",
                "search": tool.pop("_tool_search", False),
            }
            if wire in identities and identities[wire] != identity:
                raise ValueError("Ambiguous tool catalog.")
            identities[wire] = identity
            tool["name"] = wire
            flattened.append(tool)
    # Original providers can echo ns.name or an unambiguous bare name. Neither alias
    # may shadow another declared identity; helper names never gain a bare alias.
    aliases = {}
    helpers = {
        "exec",
        "apply_patch",
        "exec_command",
        "write_stdin",
        "shell_command",
        "view_image",
        "create_goal",
        "get_goal",
        "update_goal",
    }
    for identity in identities.values():
        if "namespace" not in identity:
            continue
        candidates = [f"{identity['namespace']}.{identity['name']}"]
        if identity["name"] not in helpers:
            candidates.append(identity["name"])
        for alias in candidates:
            if alias in aliases and aliases[alias] != identity:
                aliases[alias] = None
            elif alias not in aliases:
                aliases[alias] = identity
    for alias, identity in aliases.items():
        if identity and alias not in identities:
            identities[alias] = identity
    return flattened, identities


def response_tool_identity(name, tool_names=None):
    identity = (tool_names or {}).get(name, {"name": name})
    return {key: value for key, value in identity.items() if key in {"name", "namespace"}}


def text_content(content):
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return "".join(
        p.get("text", "") for p in content if p.get("type") in {"text", "input_text", "output_text"}
    )


def chat_content(content):
    if isinstance(content, str) or content is None:
        return content
    result = []
    for part in content:
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            result.append({"type": "text", "text": part.get("text", "")})
        elif kind == "input_image":
            result.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": part["image_url"],
                        **({"detail": part["detail"]} if "detail" in part else {}),
                    },
                }
            )
        elif kind in {"image_url", "input_audio"}:
            result.append(copy.deepcopy(part))
        elif kind == "image":
            source = part["source"]
            url = source.get("url") or f"data:{source['media_type']};base64,{source['data']}"
            result.append({"type": "image_url", "image_url": {"url": url}})
        else:
            raise ValueError(f"The translated route cannot represent content type {kind!r}.")
    return result


def responses_to_chat(body, *, preserve_tool_media=False):
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
        if kind == "additional_tools":
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
            reasoning += text_content(item.get("summary", [])) or "".join(
                p.get("text", "") for p in item.get("summary", [])
            )
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
        result["tool_choice"] = (
            {"type": "tool", "name": choice["function"]["name"]}
            if isinstance(choice, dict)
            else {"type": {"required": "any", "auto": "auto", "none": "none"}[choice]}
        )
        if body.get("parallel_tool_calls") is False and result["tool_choice"]["type"] != "none":
            result["tool_choice"]["disable_parallel_tool_use"] = True
    elif result.get("tools") and body.get("parallel_tool_calls") is False:
        result["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    fmt = body.get("response_format", {})
    if fmt.get("type") == "json_schema" and fmt.get("json_schema", {}).get("schema"):
        result["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema(fmt["json_schema"]["schema"])}
        }
    return result


def to_chat(body, protocol, *, preserve_tool_media=False):
    if protocol == "responses":
        return responses_to_chat(body, preserve_tool_media=preserve_tool_media)
    if protocol == "messages":
        return messages_to_chat(body)
    return copy.deepcopy(body)


def from_chat(body, protocol):
    if protocol == "responses":
        return chat_to_responses(body)
    if protocol == "messages":
        return chat_to_messages(body)
    return body


def response_to_chat(data, protocol, *, progress_updates=False, hide_thinking=False):
    if protocol == "chat/completions":
        return data
    message = {"role": "assistant", "content": ""}
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
                        **thinking_detail(part, len(reasoning_details)),
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
