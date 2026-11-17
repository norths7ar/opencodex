"""One physical HTTP send with caller-owned cancellation and connection cleanup."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import cast

import anyio
import httpx
from pydantic import JsonValue

from backend.requests import CancellationSignal, WireProtocol


async def finish_cleanup[T](operation: Awaitable[T]) -> T:
    """Finish owned cleanup even when the caller receives repeated native cancels."""
    task = asyncio.ensure_future(operation)
    interrupted = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


@dataclass(frozen=True, slots=True)
class PreparedSend:
    url: str
    protocol: WireProtocol
    headers: dict[str, str] = field(repr=False)
    body: JsonValue = field(repr=False)
    channel_id: str = "public"
    allow_private: bool = False


async def until_cancelled[T](
    operation: Awaitable[T], cancellation: CancellationSignal, *, discard=None
) -> T:
    work = asyncio.ensure_future(operation)
    cancelled = asyncio.ensure_future(cancellation.wait())
    transferred = False
    try:
        done, _ = await asyncio.wait((work, cancelled), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            result = await work
            transferred = True
            return result
        raise asyncio.CancelledError
    finally:

        async def cleanup():
            for task in (work, cancelled):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if (
                discard is not None
                and not transferred
                and not work.cancelled()
                and work.exception() is None
            ):
                await discard(work.result())

        cleanup_task = asyncio.create_task(cleanup())
        # A second cancellation must not abandon an acquired response during cleanup.
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        cleanup_task.result()
        if interrupted:
            if transferred and discard is not None:
                closing = asyncio.create_task(discard(work.result()))
                while not closing.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(closing)
                closing.result()
            raise asyncio.CancelledError


class HTTPTransport:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        client_for_send: Callable[[PreparedSend], httpx.AsyncClient] | None = None,
    ):
        self.client = client
        self.client_for_send = client_for_send

    @asynccontextmanager
    async def open(self, prepared: PreparedSend, cancellation: CancellationSignal):
        # Construction supplies either a fixed client or a destination-scoped factory.
        client = (
            self.client_for_send(prepared)
            if self.client_for_send
            else cast(httpx.AsyncClient, self.client)
        )
        if prepared.protocol == "alpha/search" and prepared.body is None:
            request = client.build_request(
                "POST", prepared.url, headers=prepared.headers, content=b"null"
            )
        else:
            request = client.build_request(
                "POST", prepared.url, headers=prepared.headers, json=prepared.body
            )
        response = await until_cancelled(
            client.send(request, stream=True),
            cancellation,
            discard=lambda response: response.aclose(),
        )
        try:
            if cancellation.is_set():
                raise asyncio.CancelledError
            yield response
        finally:
            await finish_cleanup(response.aclose())

    @staticmethod
    async def chunks(
        response: httpx.Response, cancellation: CancellationSignal
    ) -> AsyncIterator[bytes]:
        iterator = response.aiter_bytes().__aiter__()
        while True:
            try:
                chunk = await until_cancelled(anext(iterator), cancellation)
                if cancellation.is_set():
                    raise asyncio.CancelledError
                yield chunk
            except StopAsyncIteration:
                return
