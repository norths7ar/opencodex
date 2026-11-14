"""Cancellable search iteration progress with upstream-inactivity, not total-duration, limits."""

import asyncio
import contextlib
import inspect


async def iteration_events(source, stall_ms, *, heartbeat_seconds=1.0):
    """Yield callback events while awaiting I/O; only callback progress resets inactivity.

    Callback items are native canonical Responses SSE events, heartbeat (actual upstream byte
    progress), or final with a canonical response. Awaitable dict remains supported for mocks.
    Closing this iterator closes/cancels the owned callback before returning.
    """
    if inspect.isawaitable(source):

        async def wrapped():
            yield {"type": "final", "response": await source}

        iterator = wrapped().__aiter__()
    else:
        iterator = source.__aiter__()
    task = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + stall_ms / 1000
    try:
        while True:
            if task is None:
                task = asyncio.create_task(anext(iterator))
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Routed search model stopped making progress")
            done, _ = await asyncio.wait({task}, timeout=min(remaining, heartbeat_seconds))
            if not done:
                yield {"type": "heartbeat", "local": True}
                continue
            try:
                event = task.result()
            except StopAsyncIteration:
                return
            finally:
                task = None
            deadline = loop.time() + stall_ms / 1000
            yield event
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await task
        close = getattr(iterator, "aclose", None)
        if close:
            await close()


class LeadingProgress:
    """Only the validated leading text/reasoning surface may stream before search interception."""

    def __init__(self, enabled):
        self.open = enabled
        self.text_items = set()
        self.events = []

    def accept(self, event):
        kind = event.get("type", "")
        if not self.open or kind in {"heartbeat", "response.created", "response.in_progress"}:
            return False
        if kind == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") in {"message", "reasoning"}:
                self.text_items.add(item.get("id"))
            else:
                self.open = False
                return False
        elif kind == "response.output_item.done":
            if event.get("item", {}).get("id") not in self.text_items:
                self.open = False
                return False
        elif not (
            event.get("item_id") in self.text_items
            and kind.startswith(
                ("response.output_text.", "response.content_part.", "response.reasoning_summary_")
            )
        ):
            self.open = False
            return False
        self.events.append(event)
        return True
