"""Portable compaction orchestration over injected, accounted internal sends."""

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from backend.policies.compaction import (
    COMPACT_PROMPT,
    remote_checkpoint,
    replay_projection,
    routing_override,
    summary_item,
    summary_text,
    user_item,
    v1_output,
)
from backend.policies.fallback import FallbackTarget, prepare_retry, retry_headers
from backend.protocols import sse

from .context import RequestContext, RequestPurpose
from .delivery import DeliveryState
from .transport import until_cancelled


@dataclass(slots=True)
class CompactionResponse:
    status: int
    headers: dict[str, str]
    state: DeliveryState
    content: bytes | None = None
    stream: object | None = None


def child_context(
    parent: RequestContext, body: dict, protocol: str, purpose: RequestPurpose
) -> RequestContext:
    return RequestContext(
        request_id=str(uuid4()),
        protocol=protocol,
        requested_model=body.get("model", parent.requested_model),
        streaming=bool(body.get("stream")),
        cancellation=parent.cancellation,
        purpose=purpose,
        client_id=parent.client_id,
        parent_request_id=parent.request_id,
        workflow_id=parent.workflow_id,
        conversation_id=parent.conversation_id,
        requested_service_tier=parent.requested_service_tier,
    )


@asynccontextmanager
async def dispatch_compaction(
    request,
    original,
    headers,
    configuration,
    route,
    expand,
    native_supported,
    execute,
    *,
    borrowed_lease=None,
    save_response=None,
    logical_source=None,
    binding_lookup=None,
):
    """Yield None when inapplicable, otherwise a native or portable response.

    route(selector) returns a Route; expand(body, source) asynchronously returns
    history-expanded body; native_supported(source,target,compact) determines
    upstream capability; execute(context,body,headers,borrowed_lease=) owns each
    accounted send. The yielded native response remains open until context exit.
    """
    if request.cancellation.is_set():
        raise asyncio.CancelledError
    compact = request.protocol == "responses/compact"
    remote = remote_checkpoint(original)
    body = copy.deepcopy(original)
    override = routing_override(body, headers, configuration, compact=compact)
    if not compact and not remote and override is None:
        yield None
        return
    logical = route(logical_source or original.get("model"))
    logical_selector = f"{logical.channel.id}/{logical.model_id}"
    source = logical
    binding = binding_lookup(original.get("previous_response_id")) if binding_lookup else None
    if binding and binding[0] == logical_selector:
        source = route(binding[1])
        if override is None:
            body["model"] = binding[1]
    target = route(body.get("model"))
    source_target = FallbackTarget(source.channel.id, source.model_id)
    target_target = FallbackTarget(target.channel.id, target.model_id)
    changed = source_target != target_target
    child_headers = retry_headers(headers) if source.channel.id != target.channel.id else headers
    if body.get("previous_response_id") and source.model_id != target.model_id:
        body = await until_cancelled(expand(body, source), request.cancellation)
        body.pop("previous_response_id", None)
    if not compact and not remote:
        if changed:
            expanded = await until_cancelled(expand(body, source), request.cancellation)
            body = prepare_retry(body, source_target, target_target, expanded_body=expanded)
        child = child_context(request, body, "responses", RequestPurpose.COMPACTION)
        async with execute(child, body, child_headers, borrowed_lease=borrowed_lease) as response:
            yield response
        return
    if native_supported(source, target, compact):
        child = child_context(request, body, request.protocol, RequestPurpose.COMPACTION)
        async with execute(child, body, headers, borrowed_lease=borrowed_lease) as response:
            if not compact or response.status != 404:
                yield response
                return
    expanded = await until_cancelled(expand(body, source), request.cancellation)
    original_items = expanded.get("input", [])
    if isinstance(original_items, str):
        original_items = [user_item(original_items)]
    summary_body = replay_projection(expanded, opaque_note=True, frame=False)
    if changed:
        summary_body = prepare_retry(
            summary_body, source_target, target_target, expanded_body=summary_body
        )
    summary_body.pop("previous_response_id", None)
    items = summary_body.get("input", [])
    if isinstance(items, str):
        items = [user_item(items)]
    summary_body["input"] = [
        item
        for item in items
        if not isinstance(item, dict) or item.get("type") != "compaction_trigger"
    ] + [user_item(COMPACT_PROMPT)]
    for key in (
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "text",
        "response_format",
        "conversation",
        "context_management",
    ):
        summary_body.pop(key, None)
    summary_body["stream"] = False
    summary_body["store"] = False
    child = child_context(request, summary_body, "responses", RequestPurpose.COMPACTION)
    async with execute(
        child, summary_body, child_headers, borrowed_lease=borrowed_lease
    ) as response:
        if response.status >= 400:
            yield response
            return
        if response.content is None:
            raise ValueError("Compaction summary did not return a buffered response.")
        result = json.loads(response.content)
        summary = summary_text(result)
        output = v1_output(original_items, summary) if compact else [summary_item(summary)]
        result = {
            "id": ("cmp_" if compact else "resp_ocxcmp_") + uuid4().hex,
            "object": "response.compaction" if compact else "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": target.model_id,
            "output": output,
            "usage": result.get("usage"),
        }
        if not compact and save_response is not None:
            checkpoint = {"input": [], "store": original.get("store", True)}
            origin = logical_selector if source_target.selector != logical_selector else None
            if request.cancellation.is_set():
                raise asyncio.CancelledError
            await until_cancelled(
                save_response(source, checkpoint, result, fallback_origin=origin),
                request.cancellation,
            )
        state = DeliveryState(
            result=result, terminal="completed", logical_status=200, usage=result.get("usage")
        )
        if not compact and original.get("stream"):

            async def events():
                initial = {**result, "status": "in_progress", "output": []}
                for sequence, event in enumerate(
                    [
                        {"type": "response.created", "response": initial},
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": output[0],
                        },
                        {"type": "response.output_item.done", "output_index": 0, "item": output[0]},
                        {"type": "response.completed", "response": result},
                    ]
                ):
                    if request.cancellation.is_set():
                        raise asyncio.CancelledError
                    event["sequence_number"] = sequence
                    yield sse(event, event["type"])

            yield CompactionResponse(
                200, {"content-type": "text/event-stream"}, state, stream=events()
            )
        else:
            yield CompactionResponse(
                200,
                {"content-type": "application/json"},
                state,
                content=json.dumps(result).encode(),
            )
