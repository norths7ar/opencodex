"""Translate and inspect upstream output without taking ownership of its connection."""

import json
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from backend.policies.fallback import upstream_error_code
from backend.protocols import (
    StreamEncoder,
    chat_chunks,
    events,
    response_from_chat,
    response_to_chat,
    sse,
)

from .delivery_sse import ByteLines, SSEObserver, UpstreamBodyOverflow
from .preparation import Inbound, PreparedGeneration, Wire

Terminal = Literal["completed", "incomplete", "failed"]
MAX_BUFFERED_RESPONSE_BYTES = 16 * 1024 * 1024
_RESPONSE_TERMINALS = {
    "response.completed": "completed",
    "response.incomplete": "incomplete",
    "response.failed": "failed",
}
_TRANSLATION_FAILED = "Upstream response was interrupted or could not be translated."
_LOGICAL_ERRORS = {
    "usage_limit_reached": 429,
    "rate_limit_exceeded": 429,
    "insufficient_quota": 429,
    "insufficient_credits": 402,
    "invalid_api_key": 401,
    "authentication_error": 401,
    "permission_denied": 403,
    "invalid_request_error": 400,
    "invalid_prompt": 400,
}


class InvalidUpstreamResponse(ValueError):
    """A buffered response could not be assembled or translated."""

    status = 502
    code = "invalid_upstream_response"


@dataclass(slots=True)
class DeliveryState:
    result: dict | None = field(default=None, repr=False)
    chat: dict | None = field(default=None, repr=False)
    terminal: Terminal | None = None
    error_code: str | None = None
    logical_status: int | None = None
    usage: dict | None = None
    _message_stop_reason: str | None = field(default=None, init=False, repr=False)

    def _merge_usage(self, value) -> None:
        if isinstance(value, dict) and value:
            self.usage = {**(self.usage or {}), **value}

    def _terminal(self, status: Terminal, *, code: str | None = None, normal: bool = True) -> None:
        if self.terminal is not None:
            return
        self.terminal = status
        self.error_code = code
        self.logical_status = 200 if normal else _LOGICAL_ERRORS.get(code, 502)

    def _observe_response(self, value: dict, status: Terminal) -> None:
        self.result = value
        self._merge_usage(value.get("usage"))
        error = value.get("error")
        details = value.get("incomplete_details")
        code = error.get("code") or error.get("type") if isinstance(error, dict) else None
        if status == "incomplete" and isinstance(details, dict):
            code = details.get("reason") or code
        code = code if isinstance(code, str) else None
        self._terminal(
            status,
            code=code,
            normal=status == "completed"
            or (status == "incomplete" and code == "max_output_tokens"),
        )

    def _chat_finish(self, finish) -> None:
        if not isinstance(finish, str) or not finish:
            return
        if finish == "length":
            self._terminal("incomplete")
        elif finish == "adapter_eof":
            self._terminal("incomplete", code="adapter_eof", normal=False)
        elif finish == "error":
            self._terminal("failed", code="upstream_error", normal=False)
        else:
            self._terminal("completed")

    def observe(self, value: dict, wire: Wire, *, buffered: bool = False) -> None:
        """Inspect protocol facts; an ordinary socket EOF is not a terminal."""
        kind = value.get("type")
        self._merge_usage(value.get("usage"))
        if wire == "responses":
            response = value.get("response")
            if isinstance(response, dict):
                self._merge_usage(response.get("usage"))
            if isinstance(kind, str) and kind in _RESPONSE_TERMINALS:
                status = _RESPONSE_TERMINALS[kind]
                if isinstance(response, dict):
                    self._observe_response(response, status)
                else:
                    self._terminal("failed", code="invalid_upstream_response", normal=False)
                return
            if buffered:
                status = value.get("status")
                if status in ("completed", "incomplete", "failed"):
                    self._observe_response(value, status)
                elif not value.get("error") and value.get("object") == "response.compaction":
                    self.result = value
                    self._terminal("completed")
        elif wire == "messages":
            message = value.get("message")
            if isinstance(message, dict):
                self._merge_usage(message.get("usage"))
            delta = value.get("delta")
            if kind == "message_delta" and isinstance(delta, dict):
                self._message_stop_reason = delta.get("stop_reason") or self._message_stop_reason
            if kind == "message_stop" or (buffered and value.get("type") == "message"):
                reason = self._message_stop_reason or value.get("stop_reason")
                if reason == "error":
                    self._terminal("failed", code="upstream_error", normal=False)
                else:
                    self._terminal("incomplete" if reason == "max_tokens" else "completed")
                if buffered:
                    self.result = value
        elif wire == "chat/completions":
            if kind == "ocx.done":
                self._terminal("completed")
            choices = value.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if isinstance(choice, dict):
                        self._chat_finish(choice.get("finish_reason"))
            if buffered and self.terminal is not None:
                self.result = value
        if kind == "error" or value.get("error"):
            error = value.get("error") or value
            code = error.get("code") or error.get("type") if isinstance(error, dict) else None
            self._terminal(
                "failed",
                code=code if isinstance(code, str) else "upstream_error",
                normal=False,
            )


@dataclass(frozen=True, slots=True)
class BufferedDelivery:
    status_code: int
    state: DeliveryState
    payload: dict | None = field(default=None, repr=False)
    content: bytes | None = field(default=None, repr=False)


def _wire(inbound: Inbound) -> Wire:
    if inbound == "responses/compact":
        return "responses"
    if inbound == "messages/count_tokens":
        return "messages"
    return inbound


def _json_object(content: bytes) -> dict:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise InvalidUpstreamResponse("Expected a JSON response object.")
    return value


async def bounded_body(chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > MAX_BUFFERED_RESPONSE_BYTES:
            raise UpstreamBodyOverflow()
        yield chunk


async def read_buffered_body(chunks: AsyncIterable[bytes]) -> bytes:
    content = bytearray()
    async for chunk in bounded_body(chunks):
        content.extend(chunk)
    return bytes(content)


async def buffered_delivery(
    prepared: PreparedGeneration,
    upstream: httpx.Response,
    inbound: Inbound,
    *,
    source: AsyncIterable[bytes] | None = None,
) -> BufferedDelivery:
    """Buffer once, preserving raw responses unless a conversion is required."""
    chunks = upstream.aiter_bytes() if source is None else source
    state = DeliveryState()
    try:
        if upstream.is_error or upstream.is_redirect:
            content = await read_buffered_body(chunks)
            if upstream.is_error:
                code = upstream_error_code(content)
                state.terminal = "failed"
                # Store only known refusal codes; a provider can echo request data here.
                if code in _LOGICAL_ERRORS:
                    state.error_code = code
            return BufferedDelivery(upstream.status_code, state, content=content)
        if prepared.response_mode == "event_stream":
            async for event in events(ByteLines(bounded_body(chunks))):
                state.observe(event, "responses")
            result = state.result
            if result is None:
                raise InvalidUpstreamResponse("ChatGPT stream ended without a final response.")
            if state.terminal == "failed" or result.get("status") == "failed":
                return BufferedDelivery(
                    502,
                    state,
                    payload={
                        "error": result.get("error") or {"message": "ChatGPT response failed."}
                    },
                )
        else:
            content = await read_buffered_body(chunks)
            if not prepared.translated and prepared.native_tools is None:
                try:
                    state.observe(_json_object(content), prepared.wire, buffered=True)
                except ValueError, RecursionError:
                    # Transparent native responses are not rewritten just for inspection.
                    if prepared.stateless:
                        raise
                return BufferedDelivery(upstream.status_code, state, content=content)
            result = _json_object(content)
        if prepared.translated:
            chat = response_to_chat(
                result,
                prepared.wire,
                progress_updates=prepared.progress_updates,
                hide_thinking=prepared.hide_thinking,
            )
            result = response_from_chat(
                chat,
                _wire(inbound),
                custom_tools=prepared.custom_tools,
                tool_names=prepared.tool_names,
            )
            state = DeliveryState(chat=chat)
            state.observe(result, _wire(inbound), buffered=True)
        elif prepared.native_tools is not None:
            result = prepared.native_tools.response(result)
            state.observe(result, prepared.wire, buffered=True)
        return BufferedDelivery(upstream.status_code, state, payload=result)
    except UpstreamBodyOverflow as exc:
        if upstream.is_error:
            # The error body is unavailable, but the received HTTP status still
            # supports the status-based fallback decision.
            raise UpstreamBodyOverflow(upstream_error_status=upstream.status_code) from exc
        raise
    except (ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
        if isinstance(exc, InvalidUpstreamResponse):
            raise
        raise InvalidUpstreamResponse(
            "Upstream returned an interrupted or invalid response."
        ) from exc


async def stream_delivery(
    prepared: PreparedGeneration,
    upstream: httpx.Response,
    inbound: Inbound,
    model: str,
    *,
    source: AsyncIterable[bytes] | None = None,
    state: DeliveryState | None = None,
    on_terminal: Callable[[DeliveryState], None] | None = None,
) -> AsyncIterator[bytes]:
    """Deliver one stream; the caller closes upstream and settles the physical attempt."""
    chunks = upstream.aiter_bytes() if source is None else source
    state = state if state is not None else DeliveryState()
    notified = False

    def notify() -> None:
        nonlocal notified
        if state.terminal is not None and not notified:
            notified = True
            if on_terminal is not None:
                on_terminal(state)

    if upstream.is_error or upstream.is_redirect:
        async for chunk in chunks:
            yield chunk
        return
    if prepared.native_tools is not None:
        async for event in events(ByteLines(chunks)):
            for restored in prepared.native_tools.events(event):
                state.observe(restored, prepared.wire)
                frame = sse(restored, restored.get("type"))
                notify()
                yield frame
        return
    if not prepared.translated:
        observer = SSEObserver()
        async for chunk in chunks:
            for event in observer.feed(chunk):
                state.observe(event, prepared.wire)
                notify()
            yield chunk
        for event in observer.feed(b"", final=True):
            state.observe(event, prepared.wire)
            notify()
        return
    wire = _wire(inbound)
    encoder = StreamEncoder(wire, model, prepared.custom_tools, prepared.tool_names)
    try:
        for frame in encoder.start():
            yield frame
        async for value in chat_chunks(
            ByteLines(chunks),
            prepared.wire,
            progress_updates=prepared.progress_updates,
            hide_thinking=prepared.hide_thinking,
        ):
            for frame in encoder.feed(value):
                yield frame
        result, frames = encoder.finish()
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
        code = (
            "upstream_timeout"
            if isinstance(exc, httpx.TimeoutException)
            else "upstream_interrupted"
            if isinstance(exc, httpx.HTTPError)
            else exc.code
            if isinstance(exc, UpstreamBodyOverflow)
            else "invalid_upstream_response"
        )
        state._terminal("failed", code=code, normal=False)
        if state.error_code == "upstream_timeout":
            state.logical_status = 504
        notify()
        yield encoder.error(_TRANSLATION_FAILED)
        return
    state.chat = encoder.data
    state.observe(result, wire, buffered=True)
    if encoder.data["choices"][0].get("finish_reason") == "adapter_eof" and wire == "messages":
        # The legacy encoder's Messages end_turn can't distinguish a truncated input.
        state.terminal, state.error_code, state.logical_status = "incomplete", "adapter_eof", 502
    notify()
    for frame in frames:
        yield frame
