"""Transport bridge for the retained hosted-search orchestration."""

import codecs
import copy
import hashlib
import json
import time
from uuid import uuid4

from fastapi.responses import JSONResponse, StreamingResponse

from .auxiliary import modalities, usable_subscription
from .protocol_streams import sse
from .search_auxiliary import search_plan
from .search_settings import accepts_model, candidate_rows
from .usage_capture import conversation_id


class SearchHTTPError(RuntimeError):
    def __init__(self, response):
        super().__init__("A search iteration could not complete.")
        self.response = response


async def response_events(response):
    """Decode actual response bytes; each delivered chunk is progress for the loop deadline."""
    if response.status_code >= 400:
        raise SearchHTTPError(response)
    if not isinstance(response, StreamingResponse):
        value = json.loads(response.body)
        yield {"type": "final", "response": value}
        return
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    iterator = response.body_iterator
    try:
        async for chunk in iterator:
            yield {"type": "heartbeat"}
            buffer += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
            buffer = buffer.replace("\r\n", "\n")
            if len(buffer) > 4 * 1024 * 1024:
                raise ValueError("Search stream frame exceeded the size limit.")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                data = "\n".join(
                    line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
                )
                if data and data != "[DONE]":
                    yield json.loads(data)
    finally:
        close = getattr(iterator, "aclose", None)
        if close:
            await close()


async def maybe_search(gateway, request, body, provider, model, issuer, *, native, expanded=False):
    if native or request.scope.get("ocx_search_bypass"):
        return body, None
    config = gateway.store.load()
    available = []
    for name in ("openai", "anthropic"):
        channel = next((p for p in config.providers if p.id == name), None)
        if (
            channel
            and not channel.disabled
            and channel.authMode == "oauth"
            and usable_subscription(gateway.channels.status(name))
        ):
            available.append(name)
    settings = getattr(config, "webSearchSidecar", {}) or {}
    plan = search_plan(body, settings, native=native, available=available)
    if plan:
        rows = await gateway.model_rows() if gateway.model_rows else []
        modes = modalities(config, provider, model, rows)
        plan["describeImages"] = modes is not None and "image" not in modes
        if settings.get("model") and not accepts_model(
            plan["backend"], plan["model"], candidate_rows(rows, available)
        ):
            plan = None
    thread = conversation_id(request.headers, body, "responses")
    principal = hashlib.sha256(
        request.headers.get("authorization", "local-loopback").encode()
    ).hexdigest()
    scope = (principal, thread) if thread else None
    if not plan:
        return gateway.search_service.replay.restore(body, scope, False), None
    if body.get("previous_response_id") and not expanded:
        body, _ = gateway.expand_history(body, provider, model, issuer)
    source_body = copy.deepcopy(body)
    body = {**body, "model": f"{provider.id}/{model}"}
    body.pop("previous_response_id", None)
    rounds = 0
    completed_rounds = []

    async def complete(payload, _plan):
        nonlocal rounds
        continuing = rounds > 0
        rounds += 1
        response = await gateway.dispatch_search_iteration(request, payload, continuing=continuing)
        async for event in response_events(response):
            if event.get("type") in {
                "response.completed",
                "response.failed",
                "response.incomplete",
            }:
                completed_rounds.append(event["response"])
                yield {"type": "final", "response": event["response"]}
            else:
                yield event

    async def dispatch(protocol, payload, helper_plan):
        payload = {**payload, "model": f"{helper_plan['backend']}/{payload['model']}"}
        response = await gateway.dispatch_search_helper(request, protocol, payload)
        async for event in response_events(response):
            if event.get("type") != "heartbeat":
                yield event

    stream = gateway.search_service.run(body, plan, complete, dispatch, scope=scope)
    response_id = "resp_ocxsearch_" + uuid4().hex
    created = int(time.time())
    emitted = {}
    indices = {}
    sequence = 0
    sent_events = set()
    sent_text = {}

    def mapped(event):
        nonlocal sequence
        event = copy.deepcopy(event)
        item = event.get("item")
        identity = item.get("id") if isinstance(item, dict) else event.get("item_id")
        if identity:
            if identity not in indices:
                indices[identity] = len(indices)
            event["output_index"] = indices[identity]
            if isinstance(item, dict):
                emitted[identity] = item
        if "response_id" in event:
            event["response_id"] = response_id
        key = (
            identity,
            event.get("content_index", event.get("summary_index", 0)),
            event.get("type"),
        )
        sent_events.add(key)
        if event.get("type", "").endswith(".delta") and isinstance(event.get("delta"), str):
            sent_text[key] = sent_text.get(key, "") + event["delta"]
        event["sequence_number"] = sequence
        sequence += 1
        return event

    def finish_item(item):
        identity = item["id"]
        was_done = (identity, 0, "response.output_item.done") in sent_events
        if identity not in emitted:
            initial = {**item, "status": "in_progress"}
            if "content" in initial:
                initial["content"] = []
            if "summary" in initial:
                initial["summary"] = []
            if item.get("type") == "function_call":
                initial["arguments"] = ""
            elif item.get("type") == "custom_tool_call":
                initial["input"] = ""
            yield {"type": "response.output_item.added", "item": initial}
        if not was_done:
            for index, part in enumerate(item.get("content", item.get("summary", []))):
                reasoning = item.get("type") == "reasoning"
                index_key = "summary_index" if reasoning else "content_index"
                part_prefix = (
                    "response.reasoning_summary_part" if reasoning else "response.content_part"
                )
                text_prefix = (
                    "response.reasoning_summary_text" if reasoning else "response.output_text"
                )
                base = {"item_id": identity, index_key: index}
                if (identity, index, part_prefix + ".added") not in sent_events:
                    yield {**base, "type": part_prefix + ".added", "part": {**part, "text": ""}}
                text = part.get("text", "")
                prior = sent_text.get((identity, index, text_prefix + ".delta"), "")
                if text.startswith(prior) and text[len(prior) :]:
                    yield {**base, "type": text_prefix + ".delta", "delta": text[len(prior) :]}
                if (identity, index, text_prefix + ".done") not in sent_events:
                    yield {**base, "type": text_prefix + ".done", "text": text}
                if (identity, index, part_prefix + ".done") not in sent_events:
                    yield {**base, "type": part_prefix + ".done", "part": part}
            if item.get("type") in {"function_call", "custom_tool_call"}:
                custom = item["type"] == "custom_tool_call"
                field = "input" if custom else "arguments"
                prefix = (
                    "response.custom_tool_call_input"
                    if custom
                    else "response.function_call_arguments"
                )
                text = item.get(field, "")
                prior = sent_text.get((identity, 0, prefix + ".delta"), "")
                if text.startswith(prior) and text[len(prior) :]:
                    yield {
                        "type": prefix + ".delta",
                        "item_id": identity,
                        "delta": text[len(prior) :],
                    }
                if (identity, 0, prefix + ".done") not in sent_events:
                    yield {"type": prefix + ".done", "item_id": identity, field: text}
            yield {"type": "response.output_item.done", "item": item}
        emitted[identity] = item

    async def output():
        started = False
        try:
            async for event in stream:
                kind = event["type"]
                while completed_rounds:
                    completed = completed_rounds.pop(0)
                    for item in completed.get("output", []):
                        if item.get("id") in emitted:
                            for value in finish_item(item):
                                value = mapped(value)
                                yield sse(value, value["type"])
                if kind == "heartbeat":
                    # Keep initial HTTP failures replaceable until semantic output exists.
                    if started:
                        yield b": keep-alive\n\n"
                    continue
                if not started:
                    started = True
                    initial = {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "in_progress",
                        "model": model,
                        "output": [],
                    }
                    value = mapped({"type": "response.created", "response": initial})
                    yield sse(value, value["type"])
                if kind == "routed_progress":
                    value = mapped(event["event"])
                    yield sse(value, value["type"])
                elif kind == "search_begin":
                    item = {"id": event["id"], "type": "web_search_call", "status": "in_progress"}
                    for value in (
                        {"type": "response.output_item.added", "item": item},
                        {"type": "response.web_search_call.in_progress", "item_id": item["id"]},
                        {"type": "response.web_search_call.searching", "item_id": item["id"]},
                    ):
                        value = mapped(value)
                        yield sse(value, value["type"])
                elif kind == "search_end":
                    item = event["item"]
                    for value in (
                        {"type": "response.web_search_call.completed", "item_id": item["id"]},
                        {"type": "response.output_item.done", "item": item},
                    ):
                        value = mapped(value)
                        yield sse(value, value["type"])
                elif kind == "final":
                    result = event["response"]
                    for item in result.get("output", []):
                        item.setdefault("id", "item_" + uuid4().hex)
                        for value in finish_item(item):
                            value = mapped(value)
                            yield sse(value, value["type"])
                    result = {
                        **result,
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "model": model,
                        "output": [emitted[key] for key in sorted(indices, key=indices.get)],
                    }
                    gateway.save_response(
                        provider,
                        model,
                        source_body,
                        result,
                        issuer,
                        request.headers,
                        request.state.fallback_origin,
                    )
                    terminal = "response." + result.get("status", "completed")
                    value = mapped({"type": terminal, "response": result})
                    yield sse(value, terminal)
                    return
        except SearchHTTPError, ValueError, TimeoutError:
            if not started:
                raise
            gateway.record_search_error(request, "search_failed", terminal="failed")
            failed = {
                "id": response_id,
                "object": "response",
                "created_at": created,
                "status": "failed",
                "model": model,
                "output": [emitted[key] for key in sorted(indices, key=indices.get)],
                "error": {"code": "search_failed", "message": "Search could not complete."},
            }
            value = mapped({"type": "response.failed", "response": failed})
            yield sse(value, value["type"])
        finally:
            await stream.aclose()

    iterator = output()
    try:
        first = await anext(iterator)
    except SearchHTTPError as exc:
        return body, exc.response
    except TimeoutError:
        gateway.record_search_error(request, "search_timeout")
        return body, JSONResponse(
            {
                "error": {
                    "message": "Search model stopped making progress.",
                    "code": "upstream_timeout",
                }
            },
            504,
        )
    except ValueError:
        gateway.record_search_error(request, "search_invalid_response")
        return body, JSONResponse(
            {
                "error": {
                    "message": "Search model returned an invalid response.",
                    "code": "upstream_failure",
                }
            },
            502,
        )
    except StopAsyncIteration:
        return body, JSONResponse({"error": {"message": "Search ended without a response."}}, 502)
    if source_body.get("stream"):

        async def chunks():
            try:
                yield first
                async for chunk in iterator:
                    yield chunk
            finally:
                await iterator.aclose()

        return body, StreamingResponse(chunks(), media_type="text/event-stream")
    final = None

    async def collected():
        yield first
        async for chunk in iterator:
            yield chunk

    try:
        async for chunk in collected():
            for line in chunk.decode().splitlines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event.get("type") in {
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                    }:
                        final = event["response"]
    finally:
        await iterator.aclose()
    return body, JSONResponse(final) if final else JSONResponse(
        {"error": {"message": "Search ended without a response."}}, 502
    )
