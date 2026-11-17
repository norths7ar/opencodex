"""Responses hosted-search orchestration, independent of gateway transport and credentials."""

import copy
import json
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import cast

from .search_executors import execute_search
from .search_format import format_results
from .search_streams import (
    CanonicalSearchResponse,
    LeadingProgress,
    SearchFunctionCall,
    iteration_events,
)
from .vision import BorrowedWorkflow

DEFAULTS = {
    "enabled": True,
    "backend": "openai",
    "reasoning": "low",
    "maxSearchesPerTurn": 3,
    "timeoutMs": 60000,
    "routedModelStallTimeoutMs": 200000,
    "streamRoutedModelOutput": False,
}
MODELS = {"openai": "gpt-5.6-luna"}
SYNTHETIC = {
    "type": "function",
    "name": "web_search",
    "description": "Search the web for current, real-world, or post-training-cutoff information. "
    "Returns a concise answer synthesized from live results, with sources. "
    "Use it whenever the user asks about recent events, versions, prices, docs, or anything you are unsure is current.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A single focused natural-language question or keywords.",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Several related queries instead of query.",
            },
        },
    },
}


def allows_search(choice):
    if choice == "none":
        return False
    if not isinstance(choice, dict):
        return True
    if choice.get("type") == "allowed_tools":
        return any(allows_search(tool) for tool in choice.get("tools", []))
    return choice.get("type") in {"web_search", "web_search_preview"} or (
        choice.get("type") == "function" and choice.get("name") == "web_search"
    )


def search_plan(body, settings, *, native=False, available=()):
    cfg = {**DEFAULTS, **settings}
    hosted = next(
        (tool for tool in body.get("tools", []) if tool.get("type") == "web_search"), None
    )
    if native or not hosted or not cfg["enabled"] or not allows_search(body.get("tool_choice")):
        return None
    if cfg["backend"] not in MODELS or cfg["backend"] not in available:
        return None
    return {
        **cfg,
        "model": cfg.get("model") or MODELS[cfg["backend"]],
        "hostedTool": copy.deepcopy(hosted),
    }


def backend_status(settings, available):
    backend = settings.get("backend", "openai")
    if backend not in MODELS:
        return {
            "backend": backend,
            "available": False,
            "reason": "Search backend has not been migrated",
        }
    return {
        "backend": backend,
        "available": backend in available,
        "reason": None if backend in available else "Sign in to the selected subscription channel",
    }


def queries(call):
    try:
        value = json.loads(call.get("arguments", "{}"))
    except ValueError, TypeError:
        return []
    if not isinstance(value, dict):
        return []
    raw_values = value.get("queries")
    values = raw_values if isinstance(raw_values, list) else []
    values = [item for item in values if isinstance(item, str) and item.strip()]
    if values:
        return values
    single = value.get("query")
    return [single] if isinstance(single, str) and single.strip() else []


def result_text(results, structured=False):
    return format_results(results, structured)


class SearchReplay:
    """Bounded process-local replay, scoped to caller principal + conversation, not serving model."""

    def __init__(self):
        self.entries = OrderedDict()

    def remember(self, scope, cell, call, output):
        if not scope or len(scope) != 2 or not all(scope):
            return
        self.entries[(*scope, cell)] = (copy.deepcopy(call), output)
        while len(self.entries) > 256:
            self.entries.popitem(last=False)

    def restore(self, body, scope, enabled):
        body = copy.deepcopy(body)
        items = body.get("input")
        if not isinstance(items, list):
            return body
        output = []
        for item in items:
            if item.get("type") != "web_search_call":
                output.append(item)
                continue
            if not enabled:
                continue
            entry = self.entries.get((*scope, item.get("id"))) if scope and all(scope) else None
            if entry:
                call, text = entry
                output.extend(
                    [
                        copy.deepcopy(call),
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": text,
                        },
                    ]
                )
            else:
                # A client-visible cell carries no result text. Do not fabricate one or rerun it.
                output.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "A web search was performed earlier; its result text is unavailable.",
                            }
                        ],
                    }
                )
        body["input"] = output
        return body


class SearchService:
    """Callbacks perform ordinary accounted gateway sends and preserve canonical reasoning items.

    complete(body, plan) yields canonical Responses events and a final canonical response.
    dispatch(protocol, body,
    plan) yields native helper SSE objects. Neither callback may admit another workflow turn.
    Events are heartbeat/search_begin/search_end/routed_progress/final. routed_progress carries
    iteration + original event; the bridge rebases item indexes/response identity. final includes
    streamedEvents so already delivered leading output is not duplicated by terminal replay.
    """

    def __init__(self):
        self.replay = SearchReplay()

    async def run(
        self, body, plan, complete, dispatch, *, scope=None
    ) -> AsyncGenerator[dict[str, object]]:
        request = self.replay.restore(body, scope, True)
        request.pop(
            "previous_response_id", None
        )  # Caller expands authoritative history before run.
        request["tools"] = [
            tool for tool in request.get("tools", []) if tool.get("type") != "web_search"
        ] + [copy.deepcopy(SYNTHETIC)]
        if (
            isinstance(request.get("tool_choice"), dict)
            and request["tool_choice"].get("type") == "web_search"
        ):
            request["tool_choice"] = {"type": "function", "name": "web_search"}
        if isinstance(request.get("input"), str):
            request["input"] = [{"role": "user", "content": request["input"]}]
        request.setdefault("input", [])
        count, empty_retries = 0, 0
        failed, cells = set(), []
        usage = {}
        limit = plan["maxSearchesPerTurn"]
        streamed = []
        for iteration in range(limit + 3):
            force = count >= limit
            if force:
                request["tool_choice"] = "none"
            yield {"type": "heartbeat"}
            response = None
            leading = LeadingProgress(plan.get("streamRoutedModelOutput", False))
            async with aclosing(
                iteration_events(
                    complete(copy.deepcopy(request), plan), plan["routedModelStallTimeoutMs"]
                )
            ) as events:
                async for event in events:
                    kind = event.get("type")
                    if kind == "heartbeat":
                        yield {"type": "heartbeat"}
                    elif kind == "final":
                        if response is not None:
                            raise ValueError("Search model returned multiple terminal responses")
                        response = cast(CanonicalSearchResponse, event["response"])
                    elif response is not None:
                        raise ValueError("Search model returned data after completion")
                    elif leading.accept(event):
                        progress = {
                            "type": "routed_progress",
                            "iteration": iteration,
                            "event": event,
                        }
                        streamed.append(progress)
                        yield progress
            if response is None:
                raise ValueError("Search model stream ended before completion")
            items = response.get("output", [])
            for key, value in response.get("usage", {}).items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
            calls = [
                cast(SearchFunctionCall, item)
                for item in items
                if item.get("type") == "function_call" and item.get("name") == "web_search"
            ]
            real_calls = any(
                item.get("type") in {"function_call", "custom_tool_call"} and item not in calls
                for item in items
            )
            terminal = response.get("status") in {"failed", "incomplete"} or any(
                part.get("type") == "refusal" for item in items for part in item.get("content", [])
            )
            if not calls or real_calls or terminal:
                visible = [item for item in items if item not in calls]
                text = any(part.get("text") for item in visible for part in item.get("content", []))
                if force and not real_calls and not terminal and not text:
                    if empty_retries == 0:
                        empty_retries += 1
                        continue
                    raise ValueError("Forced search answer produced no usable output")
                response["output"] = cells + visible
                if usage:
                    response["usage"] = {**response.get("usage", {}), **usage}
                sources = {
                    source["url"]: source
                    for cell in cells
                    for source in cell["action"].get("sources", [])
                }
                for item in visible:
                    for part in item.get("content", []):
                        part_text = part.get("text")
                        if part.get("type") == "output_text" and part_text:
                            annotations = part.setdefault("annotations", [])
                            existing = {annotation.get("url") for annotation in annotations}
                            annotations.extend(
                                {
                                    "type": "url_citation",
                                    "url": url,
                                    "title": source.get("title", url),
                                    "start_index": 0,
                                    "end_index": len(part_text),
                                }
                                for url, source in sources.items()
                                if url not in existing
                            )
                yield {"type": "final", "response": response, "streamedEvents": streamed}
                return
            request["input"].extend(copy.deepcopy(items))
            for call in calls:
                batch = queries(call)
                results = []
                cell_id = "ws_" + uuid.uuid4().hex
                began = False
                if not batch:
                    count += 1
                    results.append(
                        ("", {"error": "the model called web_search with an empty query"})
                    )
                for query in batch:
                    normalized = " ".join(query.casefold().split())
                    if normalized in failed:
                        outcome = {
                            "error": "this query already failed; answer from existing context"
                        }
                    elif count >= limit:
                        outcome = {
                            "error": "web search limit reached; answer from results already gathered"
                        }
                    else:
                        if not began:
                            began = True
                            yield {"type": "search_begin", "id": cell_id}
                        outcome = None
                        async with aclosing(
                            iteration_events(
                                execute_search(query, plan, dispatch), plan["timeoutMs"] + 1000
                            )
                        ) as events:
                            async for progress in events:
                                if progress["type"] == "final":
                                    outcome = progress["response"]
                                else:
                                    yield {"type": "heartbeat"}
                        count += 1
                        # execute_search always yields a final response on normal completion.
                        if cast(dict[str, object], outcome).get("error"):
                            failed.add(normalized)
                    results.append((query, outcome))
                    yield {"type": "heartbeat"}
                text = result_text(results, bool(request.get("text", {}).get("format")))
                request["input"].append(
                    {"type": "function_call_output", "call_id": call["call_id"], "output": text}
                )
                if began:
                    sources = {
                        source["url"]: source
                        for _, outcome in results
                        for source in outcome.get("sources", [])
                    }
                    cell = {
                        "type": "web_search_call",
                        "id": cell_id,
                        "status": "completed"
                        if any(not outcome.get("error") for _, outcome in results)
                        else "failed",
                        "action": {
                            "type": "search",
                            "queries": batch,
                            "sources": list(sources.values()),
                        },
                    }
                    cells.append(cell)
                    self.replay.remember(scope, cell_id, call, text)
                    yield {"type": "search_end", "item": cell}
        raise ValueError("Web search exceeded its iteration cap")


async def run_search(service, body, plan, context, workflow_lease, execute, *, scope=None):
    """Run accounted internal sends; execute returns decoded events for body/protocol/context/lease.

    The same parent context and lease are passed to every iteration and helper. The
    executor is responsible for child purposes, cancellation and each physical reservation.
    """

    def borrowed():
        return (
            BorrowedWorkflow(workflow_lease.ledger, workflow_lease.root_id)
            if workflow_lease
            else None
        )

    def complete(payload, _plan):
        return execute(payload, "responses", context, borrowed())

    def dispatch(protocol, payload, helper_plan):
        payload = {**payload, "model": f"{helper_plan['backend']}/{payload['model']}"}
        return execute(payload, protocol, context, borrowed())

    async with aclosing(service.run(body, plan, complete, dispatch, scope=scope)) as events:
        async for event in events:
            yield event
