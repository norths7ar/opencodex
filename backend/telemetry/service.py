"""Logical request and physical send telemetry orchestration."""

import re
from contextlib import suppress

import anyio

from backend.gateway.context import AttemptContext, Outcome, RequestContext, Usage

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


class Telemetry:
    def __init__(self, store: UsageStore, *, debug: DebugState | None = None):
        self.store = store
        self.debug = debug
        self._requests: dict[str, int | None] = {}
        self._borrowed: set[str] = set()
        self._owners: dict[str, str] = {}

    def _provider_event(self, event: str, **fields) -> None:
        if self.debug is not None:
            with suppress(OSError, ValueError, TypeError):
                self.debug.event("provider", event, **fields)

    def attempt_started(self, attempt: AttemptContext) -> None:
        self._provider_event(
            "route",
            provider=attempt.channel_id,
            model=attempt.model,
            protocol=attempt.protocol,
            reason=attempt.reason.value,
        )

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
        return row

    async def end_request(self, request: RequestContext, row, outcome, **kwargs):
        self._requests.pop(request.request_id, None)
        self._owners.pop(request.request_id, None)
        if request.request_id in self._borrowed:
            self._borrowed.remove(request.request_id)
            return
        await self.finalize_request(row, outcome, **kwargs)

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
                "sent_upstream": True,
            },
        )

    async def finalize_attempt(
        self, send_id: int | None, attempt: AttemptContext, *, close_reason: str | None = None
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
            "close_reason": close_reason,
        }
        await self.store.finalize_send(
            send_id,
            result,
            {
                "served_model": attempt.served_model,
                "response_service_tier": attempt.response_service_tier,
            },
        )

    async def finalize_request(
        self,
        request_row_id: int | None,
        outcome: Outcome,
        *,
        usage: Usage | None = None,
        duration_ms: float | None = None,
        close_reason: str | None = None,
    ) -> None:
        sends = list(self.store.attempts(request_row_id)) if request_row_id is not None else []
        await self.store.finalize(
            request_row_id,
            status=outcome.status_code if outcome.status_code is not None else 502,
            usage=usage.values if usage else None,
            usage_status=usage.status.value if usage else "unreported",
            error_code=outcome.error_code,
            cancelled=outcome.cancelled,
            terminal_status=outcome.terminal_status.value if outcome.terminal_status else None,
            duration_ms=duration_ms,
            close_reason=close_reason,
            send_count=len(sends),
            settled_sends=len(sends),
            fallback_attempts=sends,
        )
