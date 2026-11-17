"""Decode upstream streaming events into incremental Chat deltas."""

import json
from collections.abc import AsyncIterator
from typing import cast

from .anthropic_reasoning import chat_usage as anthropic_chat_usage
from .anthropic_reasoning import detail as thinking_detail
from .sse import events
from .stream_types import AnthropicEvent, ChatEvent, ResponsesEvent, ResponseTerminal, ThinkingState


def chunk(delta=None, finish=None, usage=None, *, snapshot=True) -> dict[str, object]:
    if snapshot and delta and "reasoning_details" in delta:
        delta = {**delta, "_thinking_snapshot": True}
    result: dict[str, object] = {
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    }
    if usage is not None:
        result["usage"] = usage
    return result


async def chat_chunks(
    response, protocol, *, transform=None, progress_updates=False, hide_thinking=False
) -> AsyncIterator[dict[str, object]]:
    """Decode only translated upstream streams; same-wire streams bypass this function."""
    terminal = False
    tools = {}
    anthropic_usage: dict[str, object] = {}
    thinking_blocks: dict[int, ThinkingState] = {}
    completed_thinking = set()
    async for raw_event in events(response, include_done=protocol == "chat/completions"):
        # SSE JSON is open-ended; each wire event below selects the payload shape it consumes.
        data = cast(dict[str, object], raw_event)
        if transform:
            data = transform(data)
        if data.get("error") or data.get("type") == "error":
            raise ValueError("Upstream reported an error during the response stream.")
        if protocol == "chat/completions":
            chat = cast(ChatEvent, data)
            if chat.get("type") == "ocx.done":
                if not terminal:
                    yield chunk(finish="stop")
                terminal = True
                continue
            choices = chat.get("choices", [])
            if any(c.get("finish_reason") == "error" for c in choices):
                raise ValueError("Upstream reported a failed completion.")
            if any(c.get("finish_reason") for c in choices):
                terminal = True
            yield data
            continue
        # Events without a discriminator matched no translated event branch before typing.
        if "type" not in data:
            continue
        if protocol == "responses":
            response_event = cast(ResponsesEvent, data)
            if response_event["type"] == "response.output_text.delta":
                yield chunk({"content": response_event["delta"]})
            elif response_event["type"] == "response.reasoning_summary_text.delta":
                yield chunk({"reasoning_content": response_event["delta"]})
            elif response_event["type"] == "response.output_item.added":
                item = response_event["item"]
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
            elif response_event["type"] == "response.function_call_arguments.delta":
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tools[response_event["item_id"]],
                                "function": {"arguments": response_event["delta"]},
                            }
                        ]
                    }
                )
            elif response_event["type"] in {"response.completed", "response.incomplete"}:
                terminal = True
                response_body = cast(ResponseTerminal, response_event)["response"]
                usage = response_body.get("usage") or {}
                incoming, outgoing = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                yield chunk(
                    finish="length"
                    if response_event["type"] == "response.incomplete"
                    else "tool_calls"
                    if tools
                    else "stop",
                    usage={
                        "prompt_tokens": incoming,
                        "completion_tokens": outgoing,
                        "total_tokens": incoming + outgoing,
                    },
                )
            elif response_event["type"] == "response.failed":
                raise ValueError("Upstream failed to complete the response.")
        else:
            anthropic = cast(AnthropicEvent, data)
            if anthropic["type"] == "message_start":
                message = anthropic["message"]
                message_usage = message.get("usage", {})
                message_usage.get("input_tokens", 0)
                anthropic_usage.update(message_usage)
            elif anthropic["type"] == "content_block_start":
                block = anthropic["content_block"]
                if block["type"] == "tool_use":
                    index = len(tools)
                    tools[anthropic["index"]] = index
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
                    # Both accepted thinking forms are copied into the mutable accumulator.
                    thinking_blocks[anthropic["index"]] = cast(ThinkingState, dict(block))
                    if not progress_updates and not hide_thinking:
                        yield chunk(
                            {
                                "reasoning_content": block.get("thinking", ""),
                                "reasoning_details": [thinking_detail(block, anthropic["index"])],
                            }
                        )
                elif block["type"] not in {"text", "thinking"}:
                    raise ValueError(f"Cannot translate streamed Messages block {block['type']!r}.")
            elif anthropic["type"] == "content_block_delta":
                delta = anthropic["delta"]
                if delta["type"] == "text_delta":
                    yield chunk({"content": delta["text"]})
                elif delta["type"] == "thinking_delta":
                    thinking = thinking_blocks.setdefault(
                        anthropic["index"], {"type": "thinking", "thinking": ""}
                    )
                    thinking["thinking"] = thinking.get("thinking", "") + delta["thinking"]
                    if not progress_updates and not hide_thinking:
                        yield chunk(
                            {
                                "reasoning_content": delta["thinking"],
                                "reasoning_details": [
                                    thinking_detail(thinking, anthropic["index"])
                                ],
                            }
                        )
                elif delta["type"] == "signature_delta":
                    thinking = thinking_blocks.setdefault(
                        anthropic["index"], {"type": "thinking", "thinking": ""}
                    )
                    thinking["signature"] = delta["signature"]
                    if not progress_updates and not hide_thinking:
                        yield chunk(
                            {"reasoning_details": [thinking_detail(thinking, anthropic["index"])]}
                        )
                elif delta["type"] == "input_json_delta":
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tools[anthropic["index"]],
                                    "function": {"arguments": delta["partial_json"]},
                                }
                            ]
                        }
                    )
            elif (
                anthropic["type"] == "content_block_stop"
                and anthropic.get("index") in thinking_blocks
            ):
                completed_thinking.add(anthropic["index"])
                # Only thinking/redacted blocks enter this table, so detail is non-null.
                entry = cast(
                    dict[str, object],
                    thinking_detail(thinking_blocks[anthropic["index"]], anthropic["index"]),
                )
                if progress_updates:
                    entry["_progress"] = True
                if hide_thinking:
                    entry["_hidden"] = True
                yield chunk({"reasoning_details": [entry], "_thinking_complete": True})
            elif anthropic["type"] == "message_delta":
                for index, block in thinking_blocks.items():
                    if index not in completed_thinking:
                        # Same accepted-thinking invariant as content_block_stop.
                        entry = cast(dict[str, object], thinking_detail(block, index))
                        if progress_updates:
                            entry["_progress"] = True
                        if hide_thinking:
                            entry["_hidden"] = True
                        yield chunk({"reasoning_details": [entry], "_thinking_complete": True})
                        completed_thinking.add(index)
                message_delta = anthropic.get("delta", {})
                if message_delta.get("stop_reason") == "error":
                    raise ValueError("Upstream reported a failed Messages completion.")
                anthropic_usage.update(anthropic.get("usage", {}))
                if message_delta.get("stop_reason") in {"refusal", "content_filter"}:
                    yield {"_terminal_refusal": True, "choices": []}
                stop_reasons: dict[str | None, str] = {
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                    "refusal": "content_filter",
                    "content_filter": "content_filter",
                }
                yield chunk(
                    finish=stop_reasons.get(anthropic["delta"].get("stop_reason"), "stop"),
                    usage=anthropic_chat_usage(anthropic_usage),
                )
            elif anthropic["type"] == "message_stop":
                terminal = True
    if not terminal:
        yield chunk(finish="adapter_eof")
