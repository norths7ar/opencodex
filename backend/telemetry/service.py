"""Logical request and physical send telemetry orchestration."""

import asyncio
import logging
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass

import anyio

from backend.requests import AttemptContext, Outcome, RequestContext, RequestPurpose, Usage

from .debug import DebugState
from .ledger import UsageStore, normalize_usage

_PATHS = {
    "chat/completions": "/v1/chat/completions",
    "responses": "/v1/responses",
    "responses/compact": "/v1/responses/compact",
    "messages": "/v1/messages",
    "messages/count_tokens": "/v1/messages/count_tokens",
    "alpha/search": "/v1/alpha/search",
}

_CONTENT_TYPES = {"application/json", "application/problem+json", "text/event-stream"}
_FALLBACK_TRIGGERS = {
    "connection_error",
    "rate_limited",
    "upstream_unavailable",
    "target_unavailable",
    "upstream_5xx",
}


@dataclass(frozen=True, slots=True)
class SendFacts:
    """Facts measured by the physical send, independent of durable logging."""

    close_reason: str | None
    settled: bool
    received_headers: bool
    semantic_output: bool
    output_started: bool


class Telemetry:
    def __init__(self, store: UsageStore, *, debug: DebugState | None = None, on_success=None):
        self.store = store
        self.debug = debug
        self.on_success = on_success
        self._requests: dict[str, int | None] = {}
        self._borrowed: set[str] = set()
        self._owners: dict[str, str] = {}
        self._sends: dict[str, dict[str, SendFacts | None]] = {}
        self._direct_sends: dict[str, list[str]] = {}

    def _provider_event(self, event: str, **fields) -> None:
        if self.debug is not None:
            with suppress(OSError, ValueError, TypeError):
                self.debug.event("provider", event, **fields)

    def attempt_started(self, attempt: AttemptContext) -> None:
        owner = self._owners.get(attempt.request_id)
        if owner is not None:
            self._sends[owner][attempt.attempt_id] = None
            self._direct_sends[attempt.request_id].append(attempt.attempt_id)
        self._provider_event(
            "route",
            provider=attempt.channel_id,
            model=attempt.model,
            protocol=attempt.protocol,
            reason=attempt.reason.value,
        )

    def attempt_finished(self, attempt: AttemptContext, facts: SendFacts) -> None:
        owner = self._owners.get(attempt.request_id)
        if owner is not None:
            self._sends[owner][attempt.attempt_id] = facts

    def send_count(self, request: RequestContext) -> int:
        return len(self._sends.get(self._owners.get(request.request_id), ()))

    def upstream_status(self, attempt: AttemptContext, status: int) -> None:
        self._provider_event(
            "upstream-status",
            provider=attempt.channel_id,
            model=attempt.model,
            status=status,
        )

    def attempt_failed(self, attempt: AttemptContext, *, kind: str) -> None:
        event = "upstream-error" if kind == "invalid_response" else "transport-error"
        self._provider_event(event, provider=attempt.channel_id, model=attempt.model, kind=kind)

    def fallback(self, source: str, target: str, trigger: str | None) -> None:
        self._provider_event(
            "fallback",
            source=source,
            target=target,
            trigger=trigger if trigger in _FALLBACK_TRIGGERS else "other",
        )

    async def observe_usage(
        self, attempt: AttemptContext, *, content_type: str | None, streaming: bool
    ) -> None:
        debug = self.debug
        if debug is None or not debug.enabled("usage"):
            return
        outcome = attempt.outcome
        body_type = (content_type or "").partition(";")[0].strip().lower()
        record = {
            "requestId": attempt.request_id,
            "provider": attempt.channel_id,
            "model": attempt.model,
            "upstreamStatus": outcome.status_code if outcome else None,
            "upstreamContentType": body_type if body_type in _CONTENT_TYPES else None,
            "bodyKind": "sse" if streaming else "json",
            "extractedUsage": normalize_usage(attempt.usage.values if attempt.usage else None),
        }
        # Debug records contain structural metadata and counters only. In particular, do not
        # sample the response, dump transport exceptions, or include headers in this record.
        with suppress(OSError, ValueError, TypeError):
            await anyio.to_thread.run_sync(debug.usage, record)

    async def begin_request(self, request: RequestContext) -> int | None:
        if request.parent_request_id in self._requests:
            row = self._requests[request.parent_request_id]
            self._requests[request.request_id] = row
            self._borrowed.add(request.request_id)
            self._owners[request.request_id] = self._owners[request.parent_request_id]
            self._direct_sends[request.request_id] = []
            return row
        row = await self.store.begin(
            method="POST",
            path=_PATHS[request.protocol],
            request_id=request.request_id,
            purpose=request.purpose.value,
            requested_model=request.requested_model,
            request_client=request.client_id or "unknown",
            streaming=request.streaming,
            conversation_id=request.conversation_id,
            requested_service_tier=request.requested_service_tier,
        )
        self._requests[request.request_id] = row
        self._owners[request.request_id] = request.request_id
        self._sends[request.request_id] = {}
        self._direct_sends[request.request_id] = []
        return row

    async def end_request(
        self, request: RequestContext, row, outcome, *, attempt_id: str | None = None, **kwargs
    ):
        self._requests.pop(request.request_id, None)
        self._owners.pop(request.request_id, None)
        direct = self._direct_sends.pop(request.request_id, ())
        if request.request_id in self._borrowed:
            self._borrowed.remove(request.request_id)
            return
        sends = self._sends.pop(request.request_id, {})
        if (
            attempt_id is None
            and direct
            and outcome.error_code
            in {
                "upstream_timeout",
                "upstream_connect_timeout",
                "upstream_connect_error",
                "upstream_transport_error",
                "upstream_interrupted",
                "invalid_upstream_response",
                "body_overflow",
            }
        ):
            attempt_id = direct[-1]
        facts = sends.get(attempt_id)
        successful_generation = (
            request.parent_request_id is None
            and request.purpose == RequestPurpose.GENERATION
            and request.protocol in {"responses", "chat/completions", "messages"}
            and outcome.status_code is not None
            and 200 <= outcome.status_code < 300
            and not outcome.cancelled
            and not outcome.interrupted
            and outcome.terminal_status not in {"failed", "cancelled"}
        )
        await self.finalize_request(
            row,
            outcome,
            attempt_id=attempt_id,
            facts=facts,
            send_count=len(sends),
            settled_sends=sum(item is not None and item.settled for item in sends.values()),
            successful_generation=successful_generation,
            **kwargs,
        )
        if successful_generation and self.on_success is not None:
            try:
                await anyio.to_thread.run_sync(self.on_success)
            except OSError, sqlite3.Error:
                logging.getLogger(__name__).exception(
                    "Could not persist the first successful request"
                )

    async def begin_attempt(
        self,
        request_row_id: int | None,
        request: RequestContext,
        attempt: AttemptContext,
        *,
        auth_mode: str | None = None,
        price_family: str | None = None,
    ) -> int | None:
        if request.request_id != attempt.request_id:
            raise ValueError("Attempt does not belong to request.")
        if request_row_id is None:
            return None
        timestamp = int(attempt.sent_at.timestamp() * 1000) if attempt.sent_at else None
        return await self.store.begin_send(
            request_row_id,
            self._owners.get(request.request_id, request.request_id),
            attempt.attempt_id,
            request.purpose.value,
            attempt.reason.value,
            timestamp,
            {
                "provider": attempt.channel_id,
                "model": attempt.model,
                "upstream_model": attempt.model,
                "requested_model": request.requested_model,
                "request_client": request.client_id or "unknown",
                "streaming": request.streaming,
                "requested_service_tier": request.requested_service_tier,
                "price_timestamp": timestamp,
                "auth_mode": auth_mode,
                "price_family": price_family,
                "upstream_wire": attempt.protocol,
                "parent_request_id": request.parent_request_id,
                "child_request_id": request.request_id if request.parent_request_id else None,
            },
        )

    async def finalize_attempt(
        self, send_id: int | None, attempt: AttemptContext, *, facts: SendFacts | None = None
    ) -> None:
        outcome, usage = attempt.outcome, attempt.usage
        counters = normalize_usage(usage.values if usage else None)
        result = {
            "status": 499
            if outcome and outcome.cancelled
            else outcome.status_code
            if outcome and outcome.status_code is not None
            else 502,
            "usage": counters,
            "usage_status": usage.status.value if usage and counters else "unreported",
            "error_code": outcome.error_code
            if outcome
            and isinstance(outcome.error_code, str)
            and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", outcome.error_code)
            else None,
            "terminal_status": outcome.terminal_status.value
            if outcome and outcome.terminal_status
            else None,
            "duration_ms": attempt.elapsed_ms,
            "close_reason": facts.close_reason if facts else None,
        }
        await self.store.finalize_send(
            send_id,
            result,
            {
                "served_model": attempt.served_model,
                "response_service_tier": attempt.response_service_tier,
                "sent_upstream": facts is not None,
                "response_headers_received": facts.received_headers if facts else None,
                "settled_sends": int(facts.settled) if facts else None,
                "output_started": facts.output_started if facts else None,
                "semantic_output": facts.semantic_output if facts else None,
            },
        )

    async def finalize_request(
        self,
        request_row_id: int | None,
        outcome: Outcome,
        *,
        attempt_id: str | None = None,
        usage: Usage | None = None,
        duration_ms: float | None = None,
        facts: SendFacts | None = None,
        send_count: int = 0,
        settled_sends: int = 0,
        successful_generation: bool | None = None,
    ) -> None:
        if request_row_id is None:
            return
        sends = await asyncio.to_thread(lambda: list(self.store.attempts(request_row_id)))
        # Attribute the caller's result to its selected physical send, not the
        # last ledger entry (which may belong to an auxiliary request).
        selected = next((send for send in sends if send["attempt_id"] == attempt_id), {})
        identity = {
            key: selected[key]
            for key in ("provider", "model", "upstream_model", "served_model")
            if key in selected
        }
        await self.store.finalize(
            request_row_id,
            **identity,
            status=outcome.status_code if outcome.status_code is not None else 502,
            usage=usage.values if usage else None,
            usage_status=usage.status.value if usage else "unreported",
            error_code=outcome.error_code,
            cancelled=outcome.cancelled,
            terminal_status=outcome.terminal_status.value if outcome.terminal_status else None,
            duration_ms=duration_ms,
            close_reason="client_cancel"
            if outcome.cancelled
            else facts.close_reason
            if facts
            else None,
            response_headers_received=facts.received_headers if facts else None,
            semantic_output=facts.semantic_output if facts else None,
            output_started=facts.output_started if facts else None,
            send_count=send_count,
            settled_sends=settled_sends,
            fallback_attempts=sends,
            successful_generation=successful_generation,
        )
