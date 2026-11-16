"""Physical-send lifetime: transport, observations, cancellation and settlement."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import httpx

from backend.policies.spend_calls import PhysicalSpend
from backend.requests import (
    AttemptContext,
    Outcome,
    RequestContext,
    TerminalStatus,
    Usage,
    UsageStatus,
)
from backend.telemetry.capture import UsageCapture
from backend.telemetry.ledger import normalize_usage
from backend.telemetry.service import SendFacts, Telemetry

from .delivery import MAX_BUFFERED_RESPONSE_BYTES, DeliveryState, InvalidUpstreamResponse
from .delivery_sse import UpstreamBodyOverflow
from .transport import HTTPTransport, PreparedSend, finish_cleanup


def _settled_usage(raw):
    if not raw or not all(
        any(type(raw.get(key)) is int and 0 <= raw[key] < 1 << 53 for key in keys)
        for keys in (
            ("inputTokens", "input_tokens", "prompt_tokens"),
            ("outputTokens", "output_tokens", "completion_tokens"),
        )
    ):
        return None
    return normalize_usage(raw)


class PhysicalAttempt:
    """Own the response until consumed or closed; expose observations to routing."""

    def __init__(self, transport: HTTPTransport, telemetry: Telemetry):
        self.transport = transport
        self.telemetry = telemetry
        self.capture = UsageCapture(buffered_limit=MAX_BUFFERED_RESPONSE_BYTES)
        self.context: AttemptContext | None = None
        self.response: httpx.Response | None = None
        self.exhausted = False
        self.delivery: DeliveryState | None = None
        self.facts: SendFacts | None = None

    async def chunks(self) -> AsyncIterator[bytes]:
        if self.response is None or self.context is None:
            raise RuntimeError("The physical attempt is not open.")
        async for chunk in self.transport.chunks(self.response, self.context.cancellation):
            self.capture.feed(chunk)
            yield chunk
        self.exhausted = True

    def output_started(self):
        if self.context is not None:
            self.context = replace(self.context, output_started=True)

    def finish_delivery(self, state: DeliveryState) -> None:
        self.delivery = state

    @asynccontextmanager
    async def open(
        self,
        prepared: PreparedSend,
        request: RequestContext,
        attempt: AttemptContext,
        request_row_id: int | None,
        *,
        spend: PhysicalSpend | None = None,
        auth_mode: str | None = None,
        price_family: str | None = None,
        event_stream: bool = False,
    ):
        if self.context is not None:
            raise RuntimeError("A physical attempt can send only once.")
        self.context = replace(attempt, sent_at=datetime.now(UTC))
        started = time.monotonic()
        send_id = None
        dispatched = False
        cancelled = False
        error = None
        try:
            if attempt.cancellation.is_set():
                raise asyncio.CancelledError

            async def begin():
                nonlocal send_id
                send_id = await self.telemetry.begin_attempt(
                    request_row_id,
                    request,
                    self.context,
                    auth_mode=auth_mode,
                    price_family=price_family,
                )

            await finish_cleanup(begin())
            if attempt.cancellation.is_set():
                raise asyncio.CancelledError
            if spend is not None:
                spend.dispatch()
            dispatched = True
            self.telemetry.attempt_started(self.context)
            async with self.transport.open(prepared, attempt.cancellation) as response:
                self.response = response
                self.telemetry.upstream_status(self.context, response.status_code)
                media_type = response.headers.get("content-type", "").partition(";")[0]
                self.capture.streaming = (
                    event_stream and response.is_success
                ) or media_type.strip().casefold() == "text/event-stream"
                self.capture.chat_wire = prepared.protocol == "chat/completions"
                yield self
        except asyncio.CancelledError:
            cancelled = True
            raise
        except httpx.HTTPError as exc:
            error = (
                "upstream_connect_timeout"
                if isinstance(exc, httpx.ConnectTimeout)
                else "upstream_connect_error"
                if isinstance(exc, httpx.ConnectError)
                else "upstream_timeout"
                if isinstance(exc, httpx.TimeoutException)
                else "upstream_interrupted"
                if self.response is not None
                else "upstream_transport_error"
            )
            if dispatched:
                self.telemetry.attempt_failed(
                    self.context,
                    kind="timeout" if isinstance(exc, httpx.TimeoutException) else "request_error",
                )
            raise
        except (InvalidUpstreamResponse, UpstreamBodyOverflow) as exc:
            error = exc.code
            if dispatched:
                self.telemetry.attempt_failed(self.context, kind="invalid_response")
            raise
        finally:
            self.capture.finish()
            client_cancelled = cancelled and attempt.cancellation.is_set()
            status = self.response.status_code if self.response is not None else None
            delivered_terminal = self.delivery.terminal if self.delivery is not None else None
            terminal = delivered_terminal or self.capture.terminal_status
            interrupted = bool(
                self.response is not None
                and status is not None
                and status < 400
                and (
                    (not self.exhausted and delivered_terminal is None)
                    or (self.capture.streaming and terminal is None)
                    or (self.delivery is not None and self.delivery.error_code == "adapter_eof")
                )
            )
            if cancelled:
                terminal = "cancelled"
            elif error or (status is not None and status >= 400):
                terminal = "failed"
            elif interrupted and terminal != "failed":
                terminal = "incomplete"
            elif self.exhausted and terminal is None:
                terminal = "completed"
            usage = Usage(
                UsageStatus.ESTIMATED
                if self.capture.usage and self.capture.usage.get("estimated") is True
                else UsageStatus.REPORTED
                if self.capture.usage
                else UsageStatus.UNREPORTED,
                self.capture.usage,
            )
            self.context = replace(
                self.context,
                elapsed_ms=(time.monotonic() - started) * 1000,
                outcome=Outcome(
                    status_code=status,
                    terminal_status=TerminalStatus(terminal) if terminal else None,
                    error_code=error
                    or (self.delivery.error_code if self.delivery is not None else None)
                    or self.capture.error_code,
                    cancelled=client_cancelled,
                    interrupted=interrupted,
                ),
                usage=usage,
                served_model=self.capture.served_model,
                response_service_tier=self.capture.service_tier,
            )
            delivery_error = self.context.outcome.error_code
            close_reason = (
                "client_cancel"
                if client_cancelled
                else "body_overflow"
                if delivery_error == "body_overflow"
                else "body_stall"
                if status is not None and delivery_error == "upstream_timeout"
                else "body_interrupted"
                if status is not None and (interrupted or delivery_error == "upstream_interrupted")
                else "terminal"
                if self.capture.streaming and self.capture.terminal_status is not None
                else "non_stream"
                if self.exhausted and (not self.capture.streaming or (status or 0) >= 400)
                else None
            )
            settled = close_reason in {"terminal", "non_stream"}
            complete_usage = (
                self.capture.terminal_status is not None
                if self.capture.streaming
                else self.exhausted
            )
            if dispatched:
                self.facts = SendFacts(
                    close_reason,
                    settled,
                    status is not None,
                    self.capture.first_output_ms is not None,
                    self.context.output_started,
                )
                self.telemetry.attempt_finished(self.context, self.facts)

            async def finalize():
                try:
                    if spend is not None:
                        spend.finish(_settled_usage(self.capture.usage) if complete_usage else None)
                finally:
                    await self.telemetry.finalize_attempt(send_id, self.context, facts=self.facts)
                if dispatched:
                    await self.telemetry.observe_usage(
                        self.context,
                        content_type=self.response.headers.get("content-type")
                        if self.response is not None
                        else None,
                        streaming=self.capture.streaming,
                    )

            await finish_cleanup(finalize())
