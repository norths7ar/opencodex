"""Decode upstream streaming events into incremental Chat deltas."""

import json

from .anthropic_reasoning import chat_usage as anthropic_chat_usage
from .anthropic_reasoning import detail as thinking_detail
from .sse import events


def chunk(delta=None, finish=None, usage=None, *, snapshot=True):
    if snapshot and delta and "reasoning_details" in delta:
        delta = {**delta, "_thinking_snapshot": True}
    result = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        result["usage"] = usage
    return result


async def chat_chunks(
    response, protocol, *, transform=None, progress_updates=False, hide_thinking=False
):
    """Decode only translated upstream streams; same-wire streams bypass this function."""
    terminal = False
    tools = {}
    anthropic_usage = {}
    thinking_blocks = {}
    completed_thinking = set()
    async for data in events(response, include_done=protocol == "chat/completions"):
        if transform:
            data = transform(data)
        if data.get("error") or data.get("type") == "error":
            raise ValueError("Upstream reported an error during the response stream.")
        if protocol == "chat/completions":
            if data.get("type") == "ocx.done":
                if not terminal:
                    yield chunk(finish="stop")
                terminal = True
                continue
            if any(c.get("finish_reason") == "error" for c in data.get("choices", [])):
                raise ValueError("Upstream reported a failed completion.")
            if any(c.get("finish_reason") for c in data.get("choices", [])):
                terminal = True
            yield data
            continue
        kind = data.get("type")
        if protocol == "responses":
            if kind == "response.output_text.delta":
                yield chunk({"content": data["delta"]})
            elif kind == "response.reasoning_summary_text.delta":
                yield chunk({"reasoning_content": data["delta"]})
            elif kind == "response.output_item.added":
                item = data["item"]
                if item["type"] == "function_call":
                    index = len(tools)
                    tools[item["id"]] = index
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": item["call_id"],
                                    "type": "function",
                                    "function": {
                                        "name": item["name"],
                                        "arguments": item.get("arguments", ""),
                                    },
                                }
                            ]
                        }
                    )
                elif item["type"] not in {"message", "reasoning"}:
                    raise ValueError(f"Cannot translate streamed Responses item {item['type']!r}.")
            elif kind == "response.function_call_arguments.delta":
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tools[data["item_id"]],
                                "function": {"arguments": data["delta"]},
                            }
                        ]
                    }
                )
            elif kind in {"response.completed", "response.incomplete"}:
                terminal = True
                usage = data["response"].get("usage") or {}
                incoming, outgoing = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                yield chunk(
                    finish="length"
                    if kind == "response.incomplete"
                    else "tool_calls"
                    if tools
                    else "stop",
                    usage={
                        "prompt_tokens": incoming,
                        "completion_tokens": outgoing,
                        "total_tokens": incoming + outgoing,
                    },
                )
            elif kind == "response.failed":
                raise ValueError("Upstream failed to complete the response.")
        else:
            if kind == "message_start":
                data["message"].get("usage", {}).get("input_tokens", 0)
                anthropic_usage.update(data["message"].get("usage", {}))
            elif kind == "content_block_start":
                block = data["content_block"]
                if block["type"] == "tool_use":
                    index = len(tools)
                    tools[data["index"]] = index
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": block["id"],
                                    "type": "function",
                                    "function": {
                                        "name": block["name"],
                                        "arguments": json.dumps(block["input"])
                                        if block.get("input")
                                        else "",
                                    },
                                }
                            ]
                        }
                    )
                elif block["type"] == "text" and block.get("text"):
                    yield chunk({"content": block["text"]})
                elif block["type"] in {"thinking", "redacted_thinking"}:
                    thinking_blocks[data["index"]] = dict(block)
                    if not progress_updates and not hide_thinking:
                        yield chunk(
                            {
                                "reasoning_content": block.get("thinking", ""),
                                "reasoning_details": [thinking_detail(block, data["index"])],
                            }
                        )
                elif block["type"] not in {"text", "thinking"}:
                    raise ValueError(f"Cannot translate streamed Messages block {block['type']!r}.")
            elif kind == "content_block_delta":
                delta = data["delta"]
                if delta["type"] == "text_delta":
                    yield chunk({"content": delta["text"]})
                elif delta["type"] == "thinking_delta":
                    block = thinking_blocks.setdefault(
                        data["index"], {"type": "thinking", "thinking": ""}
                    )
                    block["thinking"] = block.get("thinking", "") + delta["thinking"]
                    if not progress_updates and not hide_thinking:
                        yield chunk(
                            {
                                "reasoning_content": delta["thinking"],
                                "reasoning_details": [thinking_detail(block, data["index"])],
                            }
                        )
                elif delta["type"] == "signature_delta":
                    block = thinking_blocks.setdefault(
                        data["index"], {"type": "thinking", "thinking": ""}
                    )
                    block["signature"] = delta["signature"]
                    if not progress_updates and not hide_thinking:
                        yield chunk({"reasoning_details": [thinking_detail(block, data["index"])]})
                elif delta["type"] == "input_json_delta":
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tools[data["index"]],
                                    "function": {"arguments": delta["partial_json"]},
                                }
                            ]
                        }
                    )
            elif kind == "content_block_stop" and data.get("index") in thinking_blocks:
                completed_thinking.add(data["index"])
                entry = thinking_detail(thinking_blocks[data["index"]], data["index"])
                if progress_updates:
                    entry["_progress"] = True
                if hide_thinking:
                    entry["_hidden"] = True
                yield chunk({"reasoning_details": [entry], "_thinking_complete": True})
            elif kind == "message_delta":
                for index, block in thinking_blocks.items():
                    if index not in completed_thinking:
                        entry = thinking_detail(block, index)
                        if progress_updates:
                            entry["_progress"] = True
                        if hide_thinking:
                            entry["_hidden"] = True
                        yield chunk({"reasoning_details": [entry], "_thinking_complete": True})
                        completed_thinking.add(index)
                if data.get("delta", {}).get("stop_reason") == "error":
                    raise ValueError("Upstream reported a failed Messages completion.")
                anthropic_usage.update(data.get("usage", {}))
                if data.get("delta", {}).get("stop_reason") in {"refusal", "content_filter"}:
                    yield {"_terminal_refusal": True, "choices": []}
                yield chunk(
                    finish={
                        "max_tokens": "length",
                        "tool_use": "tool_calls",
                        "refusal": "content_filter",
                        "content_filter": "content_filter",
                    }.get(data["delta"].get("stop_reason"), "stop"),
                    usage=anthropic_chat_usage(anthropic_usage),
                )
            elif kind == "message_stop":
                terminal = True
    if not terminal:
        yield chunk(finish="adapter_eof")
