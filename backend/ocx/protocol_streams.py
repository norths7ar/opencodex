"""Incremental SSE translation. Each upstream delta is delivered once; no stream retries."""

import json
import time

from .anthropic_reasoning import chat_usage as anthropic_chat_usage
from .anthropic_reasoning import detail as thinking_detail
from .anthropic_reasoning import output_items as thinking_items
from .anthropic_reasoning import to_item as thinking_item
from .anthropic_reasoning import update_details
from .protocols import identifier, response_from_chat, response_tool_identity


def sse(data, event=None):
    prefix = f"event: {event}\n" if event else ""
    return (
        prefix + "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode()


async def events(response, *, include_done=False):
    lines = []
    async for line in response.aiter_lines():
        if not line:
            if lines:
                data = "\n".join(lines)
                lines = []
                if data == "[DONE]":
                    if include_done:
                        yield {"type": "ocx.done"}
                    return
                yield json.loads(data)
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
    if lines:
        if "\n".join(lines) == "[DONE]":
            if include_done:
                yield {"type": "ocx.done"}
        else:
            yield json.loads("\n".join(lines))


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


class StreamEncoder:
    def __init__(self, protocol, model, custom_tools=(), tool_names=None):
        self.emitted_thinking = set()
        self.protocol = protocol
        self.custom_tools = custom_tools
        self.tool_names = tool_names or {}
        self.id = identifier(
            "resp" if protocol == "responses" else "msg" if protocol == "messages" else "chatcmpl"
        )
        self.data = {
            "id": self.id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": None}
            ],
            "usage": {},
        }
        self.items = []
        self.slots = {}
        self.calls = {}
        self.sequence = 0

    def emit(self, kind, **body):
        if self.protocol == "responses":
            body = {"type": kind, "sequence_number": self.sequence, **body}
            self.sequence += 1
        else:
            body = {"type": kind, **body}
        return sse(body, kind)

    def start(self):
        if self.protocol == "responses":
            response = response_from_chat(self.data, self.protocol, self.id)
            response.update(status="in_progress", usage=None, incomplete_details=None)
            return [
                self.emit("response.created", response=response),
                self.emit("response.in_progress", response=response),
            ]
        if self.protocol == "messages":
            return [
                self.emit(
                    "message_start",
                    message={
                        "id": self.id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.data["model"],
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                )
            ]
        return [self.chat_event({"role": "assistant"})]

    def chat_event(self, delta, finish=None, usage=None):
        return sse(
            {
                "id": self.id,
                "object": "chat.completion.chunk",
                "created": self.data["created"],
                "model": self.data["model"],
                **chunk(delta, finish, usage, snapshot=False),
            }
        )

    def slot(self, key, item):
        if key in self.slots:
            return self.slots[key], []
        index = len(self.items)
        self.slots[key] = index
        self.items.append(item)
        if self.protocol == "responses":
            return index, [
                self.emit("response.output_item.added", output_index=index, item=item.copy())
            ]
        return index, [self.emit("content_block_start", index=index, content_block=item.copy())]

    def feed(self, data):
        result = []
        if data.get("_terminal_refusal"):
            self.data["_terminal_refusal"] = True
        if data.get("usage"):
            self.data["usage"] = data["usage"]
        for choice in data.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("A translated stream supports one completion.")
            if choice.get("finish_reason"):
                self.data["choices"][0]["finish_reason"] = choice["finish_reason"]
            delta = choice.get("delta") or {}
            message = self.data["choices"][0]["message"]
            details = delta.get("reasoning_details", [])
            if details:
                update_details(message, details)
                if self.protocol == "responses":
                    for entry in details:
                        if entry.get("_progress"):
                            for position, item in enumerate(
                                thinking_items(entry, identifier("rs"))
                            ):
                                _, added = self.slot(("progress", entry["index"], position), item)
                                result.extend(added)
                            continue
                        key = ("reasoning", entry["index"])
                        item = thinking_item(entry, identifier("rs"))
                        index, added = self.slot(key, item)
                        result.extend(added)
                        old = (
                            "".join(p.get("text", "") for p in self.items[index].get("summary", []))
                            if not added
                            else ""
                        )
                        text = "" if entry.get("_hidden") else entry.get("text", "")
                        self.items[index].update(thinking_item(entry, self.items[index]["id"]))
                        if text and not old:
                            result.append(
                                self.emit(
                                    "response.reasoning_summary_part.added",
                                    item_id=self.items[index]["id"],
                                    output_index=index,
                                    summary_index=0,
                                    part={"type": "summary_text", "text": ""},
                                )
                            )
                        if text[len(old) :]:
                            result.append(
                                self.emit(
                                    "response.reasoning_summary_text.delta",
                                    item_id=self.items[index]["id"],
                                    output_index=index,
                                    summary_index=0,
                                    delta=text[len(old) :],
                                )
                            )
            for field in ("content", "reasoning_content"):
                value = delta.get(field)
                if not value:
                    continue
                message[field] = message.get(field, "") + value
                if field == "reasoning_content" and details and self.protocol == "responses":
                    continue
                if self.protocol == "chat/completions":
                    continue
                reasoning = field == "reasoning_content"
                if self.protocol == "responses":
                    item = (
                        {
                            "id": identifier("rs" if reasoning else "msg"),
                            "type": "reasoning",
                            "summary": [],
                        }
                        if reasoning
                        else {
                            "id": identifier("msg"),
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        }
                    )
                    index, added = self.slot(field, item)
                    result.extend(added)
                    item = self.items[index]
                    if added:
                        result.append(
                            self.emit(
                                "response.reasoning_summary_part.added"
                                if reasoning
                                else "response.content_part.added",
                                item_id=item["id"],
                                output_index=index,
                                **(
                                    {
                                        "summary_index": 0,
                                        "part": {"type": "summary_text", "text": ""},
                                    }
                                    if reasoning
                                    else {
                                        "content_index": 0,
                                        "part": {
                                            "type": "output_text",
                                            "text": "",
                                            "annotations": [],
                                        },
                                    }
                                ),
                            )
                        )
                    result.append(
                        self.emit(
                            "response.reasoning_summary_text.delta"
                            if reasoning
                            else "response.output_text.delta",
                            item_id=item["id"],
                            output_index=index,
                            **({"summary_index": 0} if reasoning else {"content_index": 0}),
                            delta=value,
                        )
                    )
                else:
                    item = (
                        {"type": "thinking", "thinking": ""}
                        if reasoning
                        else {"type": "text", "text": ""}
                    )
                    index, added = self.slot(field, item)
                    result.extend(added)
                    result.append(
                        self.emit(
                            "content_block_delta",
                            index=index,
                            delta={
                                "type": "thinking_delta" if reasoning else "text_delta",
                                "thinking" if reasoning else "text": value,
                            },
                        )
                    )
            for tool in delta.get("tool_calls", []):
                key = tool.get("index", 0)
                call = self.calls.setdefault(
                    key, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tool.get("id"):
                    call["id"] += tool["id"]
                function = tool.get("function") or {}
                call["function"]["name"] += function.get("name", "")
                arguments = function.get("arguments", "")
                call["function"]["arguments"] += arguments
                if self.protocol == "chat/completions":
                    continue
                if not call["id"] or not call["function"]["name"]:
                    continue
                if self.protocol == "responses" and (
                    call["function"]["name"] in self.custom_tools
                    or self.tool_names.get(call["function"]["name"], {}).get("search")
                ):
                    # JSON string escaping is not the custom wire's raw-text delta format.
                    # Publish its validated input when the argument document is complete.
                    continue
                item = (
                    {
                        "id": identifier("fc"),
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": call["id"],
                        **response_tool_identity(call["function"]["name"], self.tool_names),
                        "arguments": "",
                    }
                    if self.protocol == "responses"
                    else {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": {},
                    }
                )
                index, added = self.slot(("tool", key), item)
                result.extend(added)
                if added:
                    arguments = call["function"]["arguments"]
                if arguments:
                    if self.protocol == "responses":
                        result.append(
                            self.emit(
                                "response.function_call_arguments.delta",
                                item_id=self.items[index]["id"],
                                output_index=index,
                                delta=arguments,
                            )
                        )
                    else:
                        result.append(
                            self.emit(
                                "content_block_delta",
                                index=index,
                                delta={"type": "input_json_delta", "partial_json": arguments},
                            )
                        )
            if self.protocol == "chat/completions" and delta:
                # Terminal/usage follows once the upstream has actually closed successfully.
                wire_delta = {
                    key: value
                    for key, value in delta.items()
                    if key not in {"reasoning_details", "_thinking_complete", "_thinking_snapshot"}
                }
                complete_details = [
                    entry
                    for entry in details
                    if (delta.get("_thinking_complete") or not delta.get("_thinking_snapshot"))
                    and (entry.get("signature") or entry.get("type") == "reasoning.encrypted")
                    and (
                        not delta.get("_thinking_snapshot")
                        or entry.get("index") not in self.emitted_thinking
                    )
                ]
                if complete_details:
                    wire_delta["reasoning_details"] = complete_details
                    self.emitted_thinking.update(entry.get("index") for entry in complete_details)
                if wire_delta:
                    result.append(self.chat_event(wire_delta))
        return result

    def finish(self):
        message = self.data["choices"][0]["message"]
        if self.calls:
            message["tool_calls"] = [self.calls[key] for key in sorted(self.calls)]
        final = response_from_chat(
            self.data, self.protocol, self.id, self.custom_tools, self.tool_names
        )
        result = []
        if self.protocol == "chat/completions":
            return final, [
                self.chat_event({}, self.data["choices"][0]["finish_reason"], self.data["usage"]),
                b"data: [DONE]\n\n",
            ]
        for key, index in self.slots.items():
            item = self.items[index]
            if key == "content":
                value = message["content"]
                if self.protocol == "responses":
                    part = {"type": "output_text", "text": value, "annotations": []}
                    item.update(status="completed", content=[part])
                    result.append(
                        self.emit(
                            "response.output_text.done",
                            item_id=item["id"],
                            output_index=index,
                            content_index=0,
                            text=value,
                        )
                    )
                    result.append(
                        self.emit(
                            "response.content_part.done",
                            item_id=item["id"],
                            output_index=index,
                            content_index=0,
                            part=part,
                        )
                    )
                else:
                    item["text"] = value
            elif isinstance(key, tuple) and key[0] in {"reasoning", "progress"}:
                if key[0] == "reasoning" and item.get("summary"):
                    part = item["summary"][0]
                    result.append(
                        self.emit(
                            "response.reasoning_summary_text.done",
                            item_id=item["id"],
                            output_index=index,
                            summary_index=0,
                            text=part["text"],
                        )
                    )
                    result.append(
                        self.emit(
                            "response.reasoning_summary_part.done",
                            item_id=item["id"],
                            output_index=index,
                            summary_index=0,
                            part=part,
                        )
                    )
            elif key == "reasoning_content":
                value = message[key]
                if self.protocol == "responses":
                    part = {"type": "summary_text", "text": value}
                    item["summary"] = [part]
                    result.append(
                        self.emit(
                            "response.reasoning_summary_text.done",
                            item_id=item["id"],
                            output_index=index,
                            summary_index=0,
                            text=value,
                        )
                    )
                    result.append(
                        self.emit(
                            "response.reasoning_summary_part.done",
                            item_id=item["id"],
                            output_index=index,
                            summary_index=0,
                            part=part,
                        )
                    )
                else:
                    item["thinking"] = value
            else:
                call = self.calls[key[1]]
                arguments = call["function"]["arguments"]
                if self.protocol == "responses":
                    item.update(
                        status=final["status"],
                        arguments=arguments,
                        **response_tool_identity(call["function"]["name"], self.tool_names),
                    )
                    result.append(
                        self.emit(
                            "response.function_call_arguments.done",
                            item_id=item["id"],
                            output_index=index,
                            arguments=arguments,
                        )
                    )
                else:
                    item["input"] = json.loads(arguments or "{}")
            result.append(
                self.emit("response.output_item.done", output_index=index, item=item)
                if self.protocol == "responses"
                else self.emit("content_block_stop", index=index)
            )
        if self.protocol == "responses":
            for item in final["output"]:
                if item["type"] == "tool_search_call":
                    index = len(self.items)
                    result.append(
                        self.emit(
                            "response.output_item.added",
                            output_index=index,
                            item={**item, "status": "in_progress", "arguments": {}},
                        )
                    )
                    result.append(
                        self.emit("response.output_item.done", output_index=index, item=item)
                    )
                    self.items.append(item)
                    continue
                if item["type"] != "custom_tool_call":
                    continue
                index = len(self.items)
                result.append(
                    self.emit(
                        "response.output_item.added",
                        output_index=index,
                        item={**item, "status": "in_progress", "input": ""},
                    )
                )
                result.append(
                    self.emit(
                        "response.custom_tool_call_input.delta",
                        output_index=index,
                        item_id=item["id"],
                        delta=item["input"],
                    )
                )
                result.append(
                    self.emit(
                        "response.custom_tool_call_input.done",
                        output_index=index,
                        item_id=item["id"],
                        input=item["input"],
                    )
                )
                result.append(self.emit("response.output_item.done", output_index=index, item=item))
                self.items.append(item)
            final["output"] = self.items
            result.append(
                self.emit(
                    "response.failed"
                    if final["status"] == "failed"
                    else "response.incomplete"
                    if final["status"] == "incomplete"
                    else "response.completed",
                    response=final,
                )
            )
        else:
            final["content"] = self.items
            result.extend(
                [
                    self.emit(
                        "message_delta",
                        delta={"stop_reason": final["stop_reason"], "stop_sequence": None},
                        usage=final["usage"],
                    ),
                    self.emit("message_stop"),
                ]
            )
        return final, result

    def error(self, message):
        if self.protocol == "responses":
            response = {
                "id": self.id,
                "object": "response",
                "status": "failed",
                "error": {"code": "upstream_error", "message": message},
                "output": [],
            }
            return self.emit("response.failed", response=response)
        return self.emit("error", error={"type": "api_error", "message": message})
