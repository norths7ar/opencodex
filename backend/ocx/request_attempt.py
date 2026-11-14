"""Usage, send reservations, and settlement for one logical HTTP request."""

import asyncio
import json
import logging
import sqlite3
import time
from hashlib import sha256

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import State

from .auxiliary import BorrowedWorkflow
from .fallback import retry_headers
from .spend_calls import RequestSpend
from .spend_ledger import SpendDenied
from .usage_capture import UsageCapture, classify_client, conversation_id
from .usage_pricing import pricing_family
from .usage_store import normalize_usage, now_ms
from .workflow_budget import WorkflowDenied


def retry_request(request, body, *, change_target=False):
    scope = dict(request.scope)
    if change_target:
        scope["headers"] = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in retry_headers(request.headers).items()
        ]
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {
                "type": "http.request",
                "body": json.dumps(body).encode(),
                "more_body": False,
            }
        return await request.receive()

    return Request(scope, receive)


class RequestAttempt:
    """Own accounting shared by the logical request and its same-request retries."""

    def __init__(self, request, *, usage_store, spend_ledger, workflow_budget, auxiliary, debug):
        self.request = request
        self.usage_store = usage_store
        self.spend_ledger = spend_ledger
        self.workflow_budget = workflow_budget
        self.auxiliary = auxiliary
        self.debug = debug

    def next_attempt(self, status, *, debug=False):
        state = self.request.state
        state.fallback_attempts.append(self.attempt_record(status))
        if debug:
            self.debug_usage(status)
        state.settled_sends += int(state.response_received)
        self.reset_physical_response()
        state.attempt_send_start = state.send_count
        state.attempt_settled_start = state.settled_sends

    def reset_physical_response(self):
        self.discard_response_receipt()
        self.request.state.usage_capture = UsageCapture()

    def start_send(self, spend):
        if spend:
            spend.dispatch()
        self.request.state.attempt_metadata["price_timestamp"] = now_ms()
        self.request.state.send_count += 1

    def receive_response(self):
        self.request.state.response_received = True

    def count_oauth_rejection(self, response, protocol):
        state = self.request.state
        state.settled_sends += 1
        state.usage_capture.observe(response, protocol=protocol)

    def finish_oauth_rejection(self, spend):
        if spend:
            spend.finish(normalize_usage(self.request.state.usage_capture.usage))

    def discard_response_receipt(self):
        self.request.state.response_received = False

    def search_helper(self, payload):
        child = retry_request(self.request, payload, change_target=True)
        child.scope["state"] = {}
        child.scope["ocx_search_bypass"] = True
        child.scope["ocx_search_helper"] = True
        lease = self.request.state.workflow_lease
        if lease:
            child.scope["ocx_borrowed_workflow"] = BorrowedWorkflow(lease.ledger, lease.root_id)
        return child

    def error(self, code, *, terminal=None):
        capture = self.request.state.usage_capture
        capture.error_code = code
        if terminal is not None:
            capture.terminal_status = terminal

    def attempt_record(self, status, *, settled_response=True):
        request = self.request
        capture = request.state.usage_capture
        usage = normalize_usage(capture.usage)
        metadata = getattr(request.state, "attempt_metadata", {})
        return {
            **metadata,
            "status": status,
            "usage": usage,
            "usage_status": "reported" if usage else "unreported",
            "duration_ms": round((time.monotonic() - capture.started) * 1000),
            "send_count": request.state.send_count - request.state.attempt_send_start,
            "settled_sends": request.state.settled_sends
            - request.state.attempt_settled_start
            + int(request.state.response_received and settled_response),
            "served_model": capture.served_model,
            "response_service_tier": capture.service_tier,
            "error_code": capture.error_code,
        }

    def debug_usage(self, status, *, state=None):
        state = self.request.state if state is None else State(state)
        if self.debug:
            capture = state.usage_capture
            metadata = state.attempt_metadata
            self.debug.usage(
                {
                    "requestId": str(state.usage_id),
                    "provider": metadata.get("provider"),
                    "model": metadata.get("model"),
                    "upstreamStatus": getattr(state, "upstream_status", status),
                    "upstreamContentType": getattr(state, "upstream_content_type", None),
                    "bodyKind": "sse" if capture.streaming else "json",
                    "extractedUsage": normalize_usage(capture.usage),
                    "bodySample": capture.body_sample,
                }
            )

    def snapshot(self):
        state = dict(self.request.state._state)
        state["fallback_attempts"] = list(state["fallback_attempts"])
        return state

    def sent_since(self, state):
        return self.request.state.send_count != state["send_count"]

    def restore(self, state):
        self.request.state._state.clear()
        self.request.state._state.update(state)

    async def restore_usage_metadata(self):
        if self.usage_store and self.request.state.usage_id is not None:
            await self.update_usage(**self.request.state.usage_metadata)

    async def update_usage(self, **fields):
        if self.usage_store and self.request.state.usage_id is not None:
            await self.usage_store.update(self.request.state.usage_id, **fields)

    async def route(self, provider, model, protocol, native, body, *, event):
        request = self.request
        request.state.attempt_metadata = {
            "price_timestamp": now_ms(),
            "provider": provider.id,
            "model": model,
            "upstream_model": model,
            "adapter": provider.adapter,
            "auth_mode": provider.authMode,
            "price_family": pricing_family(provider),
            "requested_service_tier": body.get("service_tier"),
        }
        if event:
            event()
        if self.usage_store and request.state.usage_id is not None:
            request.state.usage_metadata = {
                "price_timestamp": request.state.attempt_metadata["price_timestamp"],
                "provider": provider.id,
                "model": model,
                "upstream_model": model,
                "requested_model": getattr(request.state, "requested_model", body.get("model")),
                "adapter": provider.adapter,
                "streaming": bool(body.get("stream")),
                "protocol_mode": "native"
                if native == protocol.split("/compact")[0]
                else "translated",
                "auth_mode": provider.authMode,
                "upstream_wire": native,
                "conversation_id": conversation_id(request.headers, body, protocol),
                "requested_service_tier": body.get("service_tier"),
                "price_family": pricing_family(provider),
            }
            await self.usage_store.update(request.state.usage_id, **request.state.usage_metadata)

    def prepare_spend(self, outbound, native, provider, model, subscription, credential):
        request = self.request

        def reserve_count():
            self.workflow_budget.reserve_send(request.state.workflow_lease)

        tracker = request.state.spend_tracker
        if not tracker:
            reserve_count()
            return None
        scopes = {"poolId": provider.id}
        if request.state.workflow_lease:
            scopes["rootId"] = request.state.workflow_lease.root_id
        if subscription:
            scopes["identityId"] = provider.id + "/" + subscription["accountId"]
        elif credential:
            scopes["identityId"] = provider.id + "/key/" + sha256(credential.encode()).hexdigest()
        spend = tracker.prepare(outbound, native, scopes, reserve_count=reserve_count, model=model)
        request.state.spend_attempts.append((spend, request.state.usage_capture))
        return spend

    async def begin(self):
        request = self.request
        capture = UsageCapture()
        self.started = capture.started
        request.state.usage_capture = capture
        request.state.usage_id = None
        request.state.send_count = 0
        request.state.settled_sends = 0
        request.state.response_received = False
        request.state.workflow_lease = None
        request.state.fallback_attempts = []
        request.state.fallback_origin = None
        request.state.attempt_send_start = 0
        request.state.attempt_settled_start = 0
        request.state.attempt_metadata = {}
        request.state.fallback_refusal = None
        request.state.spend_tracker = RequestSpend(self.spend_ledger) if self.spend_ledger else None
        request.state.spend_attempts = []
        if self.usage_store:
            request.state.usage_id = await self.usage_store.begin(
                method=request.method,
                path=request.url.path,
                request_client=classify_client(request.headers),
            )

    async def finalize(self, status, *, cancelled=False, interrupted=False):
        request = self.request
        capture = request.state.usage_capture
        tracker = request.state.spend_tracker
        if tracker:
            try:
                try:
                    for spend, observed in request.state.spend_attempts:
                        complete = not observed.streaming or observed.terminal_status is not None
                        spend.finish(normalize_usage(observed.usage) if complete else None)
                finally:
                    tracker.close()
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
                logging.getLogger(__name__).error("Spend settlement failed: %s", type(exc).__name__)
        if request.state.workflow_lease:
            request.state.workflow_lease.release()
        self.debug_usage(status)
        if self.usage_store and request.state.usage_id is not None:
            terminal = capture.terminal_status
            if cancelled:
                terminal = "cancelled"
            elif interrupted or (capture.streaming and terminal is None and status < 400):
                terminal = "failed"
            settled = request.state.settled_sends
            if (
                request.state.response_received
                and not cancelled
                and not interrupted
                and (capture.terminal_status is not None or not capture.streaming or status >= 400)
            ):
                settled = min(request.state.send_count, settled + 1)
            try:
                attempts = request.state.fallback_attempts
                usage = capture.usage
                if attempts:
                    attempts = [
                        *attempts,
                        self.attempt_record(
                            status
                            if status >= 300
                            else capture.logical_status
                            or (502 if terminal == "failed" else status),
                            settled_response=not cancelled
                            and not interrupted
                            and (
                                capture.terminal_status is not None
                                or not capture.streaming
                                or status >= 400
                            ),
                        ),
                    ]
                    measured = [attempt["usage"] for attempt in attempts if attempt.get("usage")]
                    usage = (
                        {
                            key: sum(item.get(key, 0) for item in measured)
                            for key in {key for item in measured for key in item}
                            if key != "estimated"
                        }
                        if measured
                        else None
                    )
                await self.usage_store.update(
                    request.state.usage_id,
                    price_timestamp=request.state.attempt_metadata.get("price_timestamp"),
                    served_model=capture.served_model,
                    response_service_tier=capture.service_tier,
                    gen_start_ms=capture.generation_start_ms,
                    last_output_ms=capture.last_output_ms,
                    fallback_attempts=attempts or None,
                    fallback_refusal=request.state.fallback_refusal,
                )
                await self.usage_store.finalize(
                    request.state.usage_id,
                    status=status
                    if status >= 300
                    else capture.logical_status or (502 if terminal == "failed" else status),
                    usage=usage,
                    cancelled=cancelled,
                    terminal_status=terminal,
                    close_reason="client_cancel"
                    if cancelled
                    else "body_stall"
                    if interrupted
                    else "terminal"
                    if capture.streaming
                    else "non_stream",
                    duration_ms=round((time.monotonic() - self.started) * 1000),
                    first_token_ms=(
                        round((capture.started - self.started) * 1000) + capture.first_output_ms
                    )
                    if capture.first_output_ms is not None
                    else None,
                    error_code=capture.error_code
                    or (
                        "upstream_interrupted"
                        if interrupted
                        else {
                            400: "invalid_request",
                            401: "unauthorized",
                            403: "access_denied",
                            429: "rate_limit_exceeded",
                            502: "upstream_failure",
                            504: "upstream_timeout",
                        }.get(status)
                    ),
                    send_count=request.state.send_count,
                    settled_sends=settled,
                    sent_upstream=request.state.send_count > 0,
                )
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                logging.getLogger(__name__).error(
                    "Request usage persistence failed: %s", type(exc).__name__
                )

    async def run(self, dispatch):
        request = self.request
        await self.begin()
        try:
            borrowed = request.scope.get("ocx_borrowed_workflow")
            if borrowed is None and self.auxiliary:
                borrowed = self.auxiliary.borrow_workflow(request.headers)
            request.state.workflow_lease = borrowed or self.workflow_budget.admit_http(
                request.headers
            )
            response = await dispatch()
        except (WorkflowDenied, SpendDenied) as exc:
            request.state.usage_capture.error_code = exc.code
            response = exc.response()
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await self.finalize(
                    499 if isinstance(exc, asyncio.CancelledError) else 500,
                    cancelled=isinstance(exc, asyncio.CancelledError),
                    interrupted=True,
                )
            raise
        if isinstance(response, StreamingResponse):
            source = response.body_iterator

            async def chunks():
                cancelled = interrupted = False
                try:
                    async for chunk in source:
                        yield chunk
                except asyncio.CancelledError, GeneratorExit:
                    cancelled = True
                    raise
                except BaseException:
                    interrupted = True
                    raise
                finally:
                    with anyio.CancelScope(shield=True):
                        try:
                            if hasattr(source, "aclose"):
                                await source.aclose()
                        finally:
                            await self.finalize(
                                response.status_code, cancelled=cancelled, interrupted=interrupted
                            )

            response.body_iterator = chunks()
        else:
            await self.finalize(response.status_code)
        return response
