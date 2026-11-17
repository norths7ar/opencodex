"""Tool-shape compatibility without translating a native Responses request."""

import copy
import json
import re
from collections.abc import Callable
from typing import Protocol, cast

from .tools import response_tool_identity, response_tools


class _JsonStringDecoder(Protocol):
    # CPython exposes scanstring at runtime, but its private binding is absent from typeshed.
    scanstring: Callable[[str, int], tuple[str, int]]


def custom_item_id(value):
    return "ctc_" + value[3:] if isinstance(value, str) and value.startswith("fc_") else value


def unwrap_input(arguments):
    if not isinstance(arguments, str):
        return ""
    try:
        value = json.loads(arguments)
        if isinstance(value, dict) and isinstance(value.get("input"), str):
            return value["input"]
    except ValueError:
        pass
    return arguments


def input_prefix(arguments):
    """Decode the stable prefix of the lowered input string, including split escapes."""
    match = re.match(r'\s*\{\s*"input"\s*:\s*"', arguments)
    if not match:
        return None
    start = match.end()
    try:
        return cast(_JsonStringDecoder, json.decoder).scanstring(arguments, start)[0]
    except ValueError:
        tail = arguments[start:]
        # A partial JSON escape must not be emitted as raw text or a lone surrogate.
        while tail:
            try:
                value = json.loads('"' + tail + '"')
                return value[:-1] if value and 0xD800 <= ord(value[-1]) <= 0xDBFF else value
            except ValueError:
                tail = tail[:-1]
        return ""


class NativeToolAdapter:
    def __init__(self, body, supports_custom=None):
        self.supports_custom = supports_custom
        self.tools, self.identities = response_tools(body)
        self.converted = {
            name
            for name, identity in self.identities.items()
            if identity["custom"]
            and (identity["name"] != "apply_patch" or supports_custom is False)
        }
        declared_search = next(
            (name for name, identity in self.identities.items() if identity["search"]),
            None,
        )
        self.search_name = declared_search or "tool_search"
        if not declared_search and self.search_name in self.identities:
            self.search_name = "opencodex_tool_search"
            suffix = 2
            while self.search_name in self.identities:
                self.search_name = f"opencodex_tool_search_{suffix}"
                suffix += 1
        history_search = isinstance(body.get("input"), list) and any(
            item.get("type") in {"tool_search_call", "tool_search_output"} for item in body["input"]
        )
        choice = body.get("tool_choice")
        allows_search = choice is None or choice in ("auto", "required")
        if isinstance(choice, dict):
            allows_search = choice.get("type") == "tool_search" or (
                choice.get("type") == "allowed_tools"
                and any(tool.get("type") == "tool_search" for tool in choice.get("tools", []))
            )
        self.search_names = (
            {self.search_name} if (declared_search or history_search) and allows_search else set()
        )
        self.calls = {}
        self.sequence = 0

    def wire_name(self, item):
        namespace = item.get("namespace")
        name = item.get("name", "")
        if namespace and namespace != "functions":
            return f"{namespace}__{name}"
        if name in self.identities:
            identity = self.identities[name]
            return (
                f"{identity['namespace']}__{identity['name']}"
                if identity.get("namespace")
                else identity["name"]
            )
        return name

    def request(self, body):
        result = copy.deepcopy(body)
        tools = []
        for tool in self.tools:
            tool = copy.deepcopy(tool)
            if tool.get("name") in self.converted:
                tool.pop("format", None)
                tool["type"] = "function"
                tool["parameters"] = {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": "JavaScript source for unified exec. Use await tools.exec_command(...) for shell commands and text(...) to return textual output; do not provide a bare shell command."
                            if self.identities[tool["name"]]["name"] == "exec"
                            else "Raw input for this client-executed custom tool.",
                        }
                    },
                    "required": ["input"],
                    "additionalProperties": False,
                }
            if tool not in tools:
                tools.append(tool)
        if tools or "tools" in body:
            result["tools"] = tools
        if isinstance(result.get("input"), list):
            converted_ids = {
                item.get("call_id")
                for item in result["input"]
                if item.get("type") == "custom_tool_call"
                and (self.wire_name(item) in self.converted or self.supports_custom is False)
            }
            items = []
            for item in result["input"]:
                kind = item.get("type")
                if kind == "additional_tools":
                    continue
                if kind in {"function_call", "custom_tool_call"}:
                    item["name"] = self.wire_name(item)
                    item.pop("namespace", None)
                    if kind == "custom_tool_call" and (
                        item["name"] in self.converted or self.supports_custom is False
                    ):
                        if self.supports_custom is False:
                            identity = self.identities.get(item["name"])
                            if identity and not identity["custom"]:
                                raise ValueError(
                                    "custom_tool_compat: historical_collision: declared_function_name"
                                )
                            if (
                                not item.get("call_id")
                                or not item["name"]
                                or not isinstance(item.get("input"), str)
                            ):
                                raise ValueError(
                                    "custom_tool_compat: historical_item: invalid custom call"
                                )
                        item["type"] = "function_call"
                        item["arguments"] = json.dumps(
                            {"input": item.pop("input", "")}, ensure_ascii=False
                        )
                        item.pop("id", None)
                elif kind == "custom_tool_call_output" and item.get("call_id") in converted_ids:
                    item["type"] = "function_call_output"
                elif kind == "tool_search_call":
                    arguments = item.get("arguments", {})
                    item = {
                        **{key: value for key, value in item.items() if key != "execution"},
                        "type": "function_call",
                        "call_id": item["call_id"],
                        "name": self.search_name,
                        "arguments": arguments
                        if isinstance(arguments, str)
                        else json.dumps(arguments),
                    }
                    if isinstance(item.get("id"), str) and item["id"].startswith("tsc_"):
                        item["id"] = "fc_" + item["id"][4:]
                elif kind == "tool_search_output":
                    item = {
                        "type": "function_call_output",
                        "call_id": item["call_id"],
                        "output": json.dumps(
                            {k: v for k, v in item.items() if k in {"tools", "status"}}
                        ),
                    }
                items.append(item)
            result["input"] = items
        choice = result.get("tool_choice")
        if isinstance(choice, dict):
            selectors = (
                choice.get("tools", []) if choice.get("type") == "allowed_tools" else [choice]
            )
            for selector in selectors:
                if selector.get("type") == "tool_search":
                    selector.update(type="function", name=self.search_name)
                elif selector.get("type") in {"function", "custom"}:
                    selector["name"] = self.wire_name(selector)
                    selector.pop("namespace", None)
                    if selector["name"] in self.converted:
                        selector["type"] = "function"
        return result

    def item(self, item):
        item = copy.deepcopy(item)
        if item.get("type") not in {"function_call", "custom_tool_call"}:
            return item
        name = self.wire_name(item)
        if name in self.converted and item["type"] == "function_call":
            item.update(
                type="custom_tool_call",
                id=custom_item_id(item.get("id")),
                input=unwrap_input(item.pop("arguments", "")),
            )
        elif name in self.search_names:
            arguments = item.pop("arguments", "{}")
            item.update(
                type="tool_search_call",
                id="tsc_" + item["id"][3:]
                if isinstance(item.get("id"), str) and item["id"].startswith("fc_")
                else item.get("id"),
                execution="client",
                arguments=json.loads(arguments or "{}")
                if isinstance(arguments, str)
                else arguments,
            )
            item.pop("name", None)
            item.pop("namespace", None)
            return item
        if name in self.identities:
            item.pop("namespace", None)
            item.update(response_tool_identity(name, self.identities))
        return item

    def response(self, response):
        result = copy.deepcopy(response)
        if isinstance(result.get("output"), list):
            result["output"] = [self.item(item) for item in result["output"]]
        return result

    def events(self, event):
        event = copy.deepcopy(event)
        kind = event.get("type", "")
        result = []
        item = event.get("item")
        if kind in {"response.output_item.added", "response.output_item.done"} and isinstance(
            item, dict
        ):
            name = self.wire_name(item)
            if item.get("type") == "function_call" and (
                name in self.converted or name in self.search_names
            ):
                state = self.calls.setdefault(
                    item["id"], {"name": name, "arguments": "", "emitted": "", "done": False}
                )
                if kind.endswith(".done") and name in self.converted and not state["done"]:
                    result.extend(
                        self.finish_input(
                            event, state, item.get("arguments", state["arguments"]), item["id"]
                        )
                    )
            event["item"] = self.item(item)
        elif kind in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            state = self.calls.get(event.get("item_id"))
            if state:
                if kind.endswith(".delta"):
                    state["arguments"] += event.get("delta", "")
                    if state["name"] in self.converted:
                        prefix = input_prefix(state["arguments"])
                        if (
                            prefix is not None
                            and prefix.startswith(state["emitted"])
                            and len(prefix) > len(state["emitted"])
                        ):
                            result.append(
                                {
                                    **event,
                                    "type": "response.custom_tool_call_input.delta",
                                    "item_id": custom_item_id(event["item_id"]),
                                    "delta": prefix[len(state["emitted"]) :],
                                }
                            )
                            state["emitted"] = prefix
                elif state["name"] in self.converted:
                    result.extend(
                        self.finish_input(
                            event,
                            state,
                            event.get("arguments", state["arguments"]),
                            event["item_id"],
                        )
                    )
                return self.number(result)
        if isinstance(event.get("response"), dict):
            event["response"] = self.response(event["response"])
        result.append(event)
        return self.number(result)

    def finish_input(self, event, state, arguments, item_id):
        value = unwrap_input(arguments)
        base = {k: v for k, v in event.items() if k not in {"item", "arguments", "delta", "type"}}
        base["item_id"] = custom_item_id(item_id)
        events = []
        if value.startswith(state["emitted"]) and value != state["emitted"]:
            events.append(
                {
                    **base,
                    "type": "response.custom_tool_call_input.delta",
                    "delta": value[len(state["emitted"]) :],
                }
            )
        events.append({**base, "type": "response.custom_tool_call_input.done", "input": value})
        state.update(emitted=value, done=True)
        return events

    def number(self, events):
        for event in events:
            event["sequence_number"] = self.sequence
            self.sequence += 1
        return events
