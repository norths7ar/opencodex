"""HTTP-neutral generation execution with explicit send and response lifetimes."""

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from uuid import uuid4

import httpx

from backend.auth.access import (
    ChannelAuthentication,
    SubscriptionAuthorizationError,
    SubscriptionRefreshUnavailable,
)
from backend.configuration.models import Configuration
from backend.policies.effort import apply_effort_policy
from backend.policies.pacing import RequestPacer
from backend.requests import AttemptContext, AttemptReason, RequestContext
from backend.telemetry.pricing import pricing_family
from backend.telemetry.service import Telemetry

from .attempts import PhysicalAttempt
from .continuation import Continuations
from .delivery import DeliveryState, bounded_body, buffered_delivery, stream_delivery
from .preparation import GenerationPreparation, PreparationRoute
from .routing import Route
from .transport import HTTPTransport, PreparedSend, until_cancelled


@dataclass(slots=True)
class GenerationResponse:
    status: int
    headers: dict[str, str]
    state: DeliveryState
    attempt: PhysicalAttempt | None
    content: bytes | None = field(default=None, repr=False)
    stream: AsyncIterator[bytes] | None = field(default=None, repr=False)


class GenerationExecutor:
    """Execute one selected target, including its single OAuth refresh retry.

    Logical routing/fallback owns this context and consumes the response before
    closing it. Every retry creates a distinct PhysicalAttempt.
    """

    def __init__(
        self,
        authentication: ChannelAuthentication,
        preparation: GenerationPreparation,
        transport: HTTPTransport,
        telemetry: Telemetry,
        continuations: Continuations,
    ):
        self.authentication = authentication
        self.preparation = preparation
        self.transport = transport
        self.telemetry = telemetry
        self.continuations = continuations
        self.pacer = RequestPacer()
        self.observe_response = None

    @asynccontextmanager
    async def _send(
        self, physical, send, request, attempt, row, prepared, channel, model, subscription, reserve
    ):
        async with self.pacer.acquire(
            channel.id, model, channel.request_pacing, request.cancellation
        ):
            spend = reserve(prepared, channel, model, subscription) if reserve else None
            async with physical.open(
                send,
                request,
                attempt,
                row,
                spend=spend,
                auth_mode=channel.auth_mode,
                price_family=pricing_family(channel),
                event_stream=request.streaming or prepared.response_mode == "event_stream",
            ):
                yield

    @asynccontextmanager
    async def execute(
        self,
        configuration: Configuration,
        target: Route,
        request: RequestContext,
        body: dict,
        incoming: dict[str, str],
        request_row_id: int | None,
        *,
        reason: AttemptReason = AttemptReason.INITIAL,
        fallback_origin: str | None = None,
        metadata: dict | None = None,
        reserve: Callable | None = None,
        prepare_body: Callable[
            [dict, PreparationRoute, Route, RequestContext], Awaitable[dict | None]
        ]
        | None = None,
    ):
        if request.cancellation.is_set():
            raise asyncio.CancelledError
        channel, model = target.channel, target.model_id
        body = copy.deepcopy(body)
        apply_effort_policy(
            body, incoming, configuration, channel, model, request.protocol, metadata
        )
        route = self.preparation.route(
            channel, model, request.protocol, body, fallback_origin=fallback_origin
        )
        subscription = (
            await until_cancelled(self.authentication.token(channel), request.cancellation)
            if route.chatgpt
            else None
        )
        continuation = self.continuations.prepare(
            channel.id, model, route, body, incoming, subscription
        )
        if prepare_body is not None:
            updated = await until_cancelled(
                prepare_body(continuation.body, route, target, request), request.cancellation
            )
            if updated is not None:
                continuation.body = updated
        prepared = await until_cancelled(
            self.preparation.prepare(
                configuration,
                channel,
                model,
                route,
                continuation.body,
                incoming=continuation.headers,
                subscription=subscription,
                metadata=metadata,
            ),
            request.cancellation,
        )
        rejected_attempt = None
        for auth_attempt in range(2):
            if auth_attempt:
                if request.cancellation.is_set():
                    raise asyncio.CancelledError
                try:
                    renewed = await until_cancelled(
                        self.authentication.token(channel, rejected=subscription),
                        request.cancellation,
                    )
                except SubscriptionRefreshUnavailable as exc:
                    yield GenerationResponse(
                        exc.status,
                        {"content-type": "application/json"},
                        DeliveryState(
                            terminal="failed", logical_status=exc.status, error_code=exc.code
                        ),
                        None,
                        content=json.dumps(
                            {"error": {"code": exc.code, "message": str(exc)}}
                        ).encode(),
                    )
                    return
                except SubscriptionAuthorizationError, httpx.HTTPError:
                    # A real upstream 401 has already settled. Preserve that
                    # verdict for fallback without exposing refresh details.
                    state = DeliveryState(
                        terminal="failed", logical_status=401, error_code="upstream_error"
                    )
                    yield GenerationResponse(
                        401,
                        {"content-type": "application/json"},
                        state,
                        rejected_attempt,
                        content=b'{"error":{"code":"upstream_error","message":"Upstream authorization failed."}}',
                    )
                    return
                if renewed["accountId"] != subscription["accountId"]:
                    raise ValueError("The active ChatGPT account changed; retry the request.")
                subscription = renewed
                prepared = await until_cancelled(
                    self.preparation.prepare(
                        configuration,
                        channel,
                        model,
                        route,
                        continuation.body,
                        incoming=continuation.headers,
                        subscription=subscription,
                        metadata=metadata,
                    ),
                    request.cancellation,
                )
            attempt = AttemptContext(
                request.request_id,
                str(uuid4()),
                channel.id,
                model,
                request.protocol if route.special else prepared.wire,
                request.cancellation,
                reason=AttemptReason.AUTH_REFRESH if auth_attempt else reason,
                account_identity=continuation.issuer,
            )
            physical = PhysicalAttempt(self.transport, self.telemetry)
            send = PreparedSend(
                prepared.url,
                attempt.protocol,
                prepared.headers,
                prepared.body,
                channel.id,
                channel.allow_private_network,
            )
            async with self._send(
                physical,
                send,
                request,
                attempt,
                request_row_id,
                prepared,
                channel,
                model,
                subscription,
                reserve,
            ):
                upstream = physical.response
                if upstream is None:
                    raise RuntimeError("Upstream response was not opened.")
                if route.chatgpt and auth_attempt == 0 and upstream.status_code == 401:
                    async for _ in bounded_body(physical.chunks()):
                        pass
                    rejected_attempt = physical
                    continue
                if self.observe_response is not None:
                    self.observe_response(channel, subscription, upstream.headers, model)
                headers = (
                    self.continuations.response_headers(
                        continuation, channel.id, model, upstream.headers
                    )
                    if upstream.is_success
                    else {
                        key: upstream.headers[key]
                        for key in ("content-type", "retry-after", "x-request-id", "request-id")
                        if key in upstream.headers
                    }
                )

                def remember(state, subscription=subscription):
                    if (
                        state.terminal not in {"completed", "incomplete"}
                        or state.logical_status != 200
                    ):
                        return
                    if state.chat:
                        self.preparation.remember_reasoning(channel.id, model, state.chat)
                    active = False
                    if subscription:
                        with suppress(ValueError, RuntimeError, OSError):
                            active = (
                                self.authentication.chatgpt.status().get("activeAccountId")
                                == subscription["accountId"]
                            )
                    self.continuations.remember(
                        continuation,
                        channel.id,
                        model,
                        state.result,
                        fallback_origin=fallback_origin,
                        persist=route.stateless or route.translated,
                        observe=active,
                    )

                if not request.streaming or not upstream.is_success:
                    delivered = await buffered_delivery(
                        prepared, upstream, request.protocol, source=physical.chunks()
                    )
                    physical.finish_delivery(delivered.state)
                    remember(delivered.state)
                    content = delivered.content
                    if delivered.payload is not None:
                        content = json.dumps(delivered.payload, ensure_ascii=False).encode()
                        headers["content-type"] = "application/json"
                    if delivered.status_code >= 400 and content:
                        # Upstream errors occasionally echo the submitted credential.
                        secrets = [
                            value.removeprefix("Bearer ")
                            for key, value in prepared.headers.items()
                            if key.lower() in {"authorization", "x-api-key", "api-key"}
                        ]
                        for secret in secrets:
                            if secret:
                                content = content.replace(secret.encode(), b"[redacted]")
                    yield GenerationResponse(
                        delivered.status_code,
                        headers,
                        delivered.state,
                        physical,
                        content=content,
                    )
                    return
                state = DeliveryState()
                headers["content-type"] = "text/event-stream"
                if prepared.translated:
                    headers["cache-control"] = "no-cache"

                async def chunks(
                    prepared=prepared, upstream=upstream, physical=physical, state=state
                ):
                    try:
                        async for chunk in stream_delivery(
                            prepared,
                            upstream,
                            request.protocol,
                            model,
                            source=physical.chunks(),
                            state=state,
                            on_terminal=remember,
                        ):
                            physical.output_started()
                            yield chunk
                        if state.terminal is None:
                            state.terminal = "incomplete"
                            state.logical_status = 502
                            state.error_code = "upstream_interrupted"
                    finally:
                        physical.finish_delivery(state)

                stream = chunks()
                try:
                    yield GenerationResponse(
                        upstream.status_code, headers, state, physical, stream=stream
                    )
                finally:
                    await stream.aclose()
                return
