"""Project hosted-search orchestration onto the public Responses wire."""

import asyncio
import copy
import hashlib
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from backend.protocols import sse
from backend.telemetry.capture import conversation_id

from .auxiliary import AuxiliaryDispatch, InternalCallError
from .context import RequestContext
from .delivery import DeliveryState
from .routing import Route
from .transport import until_cancelled


@dataclass(slots=True)
class SearchResponse:
    status: int
    headers: dict[str, str]
    state: DeliveryState
    content: bytes | None = field(default=None, repr=False)
    stream: AsyncIterator[bytes] | None = field(default=None, repr=False)


def restore_search_replay(auxiliary, body, _request, _headers, *, native=False):
    """Remove locally rendered cells when the chosen route has no search bridge."""
    return body if native else auxiliary.search_service.replay.restore(body, None, False)


def _scope(request, body, headers, account_scope, principal=None):
    headers = {key.lower(): value for key, value in headers.items()}
    thread = conversation_id(headers, body, "responses") or request.conversation_id
    if not thread:
        return None
    caller = principal or headers.get("authorization", "local-loopback")
    principal = hashlib.sha256(f"{caller}\0{account_scope or ''}".encode()).hexdigest()
    return principal, thread


class _Frames:
    def __init__(self, model):
        self.id = "resp_ocxsearch_" + uuid4().hex
        self.created = int(time.time())
        self.model = model
        self.emitted = {}
        self.indices = {}
        self.sequence = 0
        self.sent_events = set()
        self.sent_text = {}

    def frame(self, original):
        event = copy.deepcopy(original)
        item = event.get("item")
        identity = item.get("id") if isinstance(item, dict) else event.get("item_id")
        if identity:
            event["output_index"] = self.indices.setdefault(identity, len(self.indices))
            if isinstance(item, dict):
                self.emitted[identity] = item
        if "response_id" in event:
            event["response_id"] = self.id
        kind = event["type"]
        key = (identity, event.get("content_index", event.get("summary_index", 0)), kind)
        self.sent_events.add(key)
        if kind.endswith(".delta") and isinstance(event.get("delta"), str):
            self.sent_text[key] = self.sent_text.get(key, "") + event["delta"]
        event["sequence_number"] = self.sequence
        self.sequence += 1
        return sse(event, kind)

    def response(self, base):
        return {
            **base,
            "id": self.id,
            "object": "response",
            "created_at": self.created,
            "model": self.model,
            "output": [self.emitted[key] for key in self.indices if key in self.emitted],
        }

    def finish_item(self, item):
        identity = item["id"]
        was_done = (identity, 0, "response.output_item.done") in self.sent_events
        if identity not in self.emitted:
            initial = {**item, "status": "in_progress"}
            for key in ("content", "summary"):
                if key in initial:
                    initial[key] = []
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
                if (identity, index, part_prefix + ".added") not in self.sent_events:
                    yield {**base, "type": part_prefix + ".added", "part": {**part, "text": ""}}
                text = part.get("text", "")
                prior = self.sent_text.get((identity, index, text_prefix + ".delta"), "")
                if text.startswith(prior) and text[len(prior) :]:
                    yield {**base, "type": text_prefix + ".delta", "delta": text[len(prior) :]}
                if (identity, index, text_prefix + ".done") not in self.sent_events:
                    yield {**base, "type": text_prefix + ".done", "text": text}
                if (identity, index, part_prefix + ".done") not in self.sent_events:
                    yield {**base, "type": part_prefix + ".done", "part": part}
            if item.get("type") in {"function_call", "custom_tool_call"}:
                custom = item["type"] == "custom_tool_call"
                key = "input" if custom else "arguments"
                prefix = (
                    "response.custom_tool_call_input"
                    if custom
                    else "response.function_call_arguments"
                )
                text = item.get(key, "")
                prior = self.sent_text.get((identity, 0, prefix + ".delta"), "")
                if text.startswith(prior) and text[len(prior) :]:
                    yield {
                        "type": prefix + ".delta",
                        "item_id": identity,
                        "delta": text[len(prior) :],
                    }
                if (identity, 0, prefix + ".done") not in self.sent_events:
                    yield {"type": prefix + ".done", "item_id": identity, key: text}
            yield {"type": "response.output_item.done", "item": item}
        self.emitted[identity] = item


_BAD_SEARCH = (InternalCallError, ValueError, TimeoutError, KeyError, TypeError, RecursionError)


def _error(status, code, message, *, content=None, headers=None):
    state = DeliveryState(terminal="failed", error_code=code, logical_status=status)
    body = (
        content
        if content is not None
        else json.dumps({"error": {"message": message, "code": code}}, ensure_ascii=False).encode()
    )
    return SearchResponse(
        status, {"content-type": "application/json", **(headers or {})}, state, content=body
    )


@asynccontextmanager
async def dispatch_search(
    auxiliary: AuxiliaryDispatch,
    request: RequestContext,
    body: dict,
    target: Route,
    headers: dict[str, str],
    lease,
    *,
    available=(),
    native=False,
    expand=None,
    save_response=None,
    account_scope=None,
    principal=None,
    helper_channel=None,
):
    """Yield a replaceable HTTP error before output, otherwise a Responses result.

    The injected callbacks expand local history and remember successful synthetic
    responses under the original target. Physical sends stay owned by AuxiliaryDispatch.
    """
    if request.cancellation.is_set():
        raise asyncio.CancelledError
    if request.protocol != "responses" or native:
        yield None
        return
    plan = auxiliary.plan_search(
        body, target, native=native, available=available, helper_channel=helper_channel
    )
    if not plan:
        yield None
        return
    source = copy.deepcopy(body)
    if source.get("previous_response_id"):
        if expand is None:
            raise ValueError("Search continuation requires local history.")
        source = await until_cancelled(expand(source, target), request.cancellation)
    routed = {**source, "model": f"{target.channel.id}/{target.model_id}"}
    routed.pop("previous_response_id", None)
    scope = _scope(request, source, headers, account_scope, principal)
    rounds = deque()
    upstream = auxiliary.search(
        routed,
        plan,
        request,
        headers,
        lease,
        scope=scope,
        on_round_complete=rounds.append,
        helper_channel=helper_channel,
    )
    wire = _Frames(target.model_id)
    state = DeliveryState()

    async def output():
        started = False
        try:
            async for event in upstream:
                if request.cancellation.is_set():
                    raise asyncio.CancelledError
                kind = event["type"]
                while rounds:
                    for item in rounds.popleft().get("output", []):
                        if item.get("id") in wire.emitted:
                            for value in wire.finish_item(item):
                                yield wire.frame(value)
                if kind == "heartbeat":
                    if started:
                        yield b": keep-alive\n\n"
                    continue
                if not started:
                    started = True
                    yield wire.frame(
                        {
                            "type": "response.created",
                            "response": wire.response({"status": "in_progress"}),
                        }
                    )
                if kind == "routed_progress":
                    yield wire.frame(event["event"])
                elif kind == "search_begin":
                    identity = event["id"]
                    item = {"id": identity, "type": "web_search_call", "status": "in_progress"}
                    yield wire.frame({"type": "response.output_item.added", "item": item})
                    for suffix in ("in_progress", "searching"):
                        yield wire.frame(
                            {"type": f"response.web_search_call.{suffix}", "item_id": identity}
                        )
                elif kind == "search_end":
                    item = event["item"]
                    yield wire.frame(
                        {"type": "response.web_search_call.completed", "item_id": item["id"]}
                    )
                    yield wire.frame({"type": "response.output_item.done", "item": item})
                elif kind == "final":
                    result = copy.deepcopy(event["response"])
                    status = result.get("status", "completed")
                    if status not in {"completed", "incomplete", "failed"}:
                        raise ValueError("Search returned a nonterminal response.")
                    for item in result.get("output", []):
                        if not item.get("id"):
                            item["id"] = "item_" + uuid4().hex
                        for value in wire.finish_item(item):
                            yield wire.frame(value)
                    result = wire.response({**result, "status": status})
                    terminal = {"type": "response." + status, "response": result}
                    if save_response is not None and (
                        status == "completed"
                        or status == "incomplete"
                        and (result.get("incomplete_details") or {}).get("reason")
                        == "max_output_tokens"
                    ):
                        if request.cancellation.is_set():
                            raise asyncio.CancelledError
                        await until_cancelled(
                            save_response(target, source, result), request.cancellation
                        )
                    state.observe(terminal, "responses")
                    yield wire.frame(terminal)
                    return
            raise ValueError("Search ended without a response.")
        except _BAD_SEARCH:
            if not started:
                raise
            failure = wire.response(
                {
                    "status": "failed",
                    "error": {"code": "search_failed", "message": "Search could not complete."},
                }
            )
            terminal = {"type": "response.failed", "response": failure}
            state.observe(terminal, "responses")
            yield wire.frame(terminal)

    async with aclosing(upstream), aclosing(output()) as iterator:
        try:
            first = await anext(iterator)
        except InternalCallError as exc:
            yield _error(
                exc.status, "upstream_failure", str(exc), content=exc.content, headers=exc.headers
            )
            return
        except TimeoutError:
            yield _error(504, "upstream_timeout", "Search model stopped making progress.")
            return
        except _BAD_SEARCH:
            yield _error(502, "upstream_failure", "Search model returned an invalid response.")
            return
        except StopAsyncIteration:
            yield _error(502, "upstream_failure", "Search ended without a response.")
            return

        if request.streaming:

            async def chunks():
                yield first
                async for frame in iterator:
                    yield frame

            async with aclosing(chunks()) as stream:
                yield SearchResponse(
                    200,
                    {"content-type": "text/event-stream", "cache-control": "no-cache"},
                    state,
                    stream=stream,
                )
        else:
            async for _ in iterator:
                pass
            if state.result is None:
                yield _error(502, "upstream_failure", "Search ended without a response.")
                return
            yield SearchResponse(
                200,
                {"content-type": "application/json"},
                state,
                content=json.dumps(state.result, ensure_ascii=False).encode(),
            )
