"""Per-channel request-start pacing and in-flight concurrency admission."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from backend.configuration.models import RequestPacing


class PacingDenied(RuntimeError):
    status = 429
    code = "request_pacing_overload"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Request pacing refused: {reason}")


@dataclass
class _State:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    queue: list = field(default_factory=list)
    active: int = 0
    model_active: dict = field(default_factory=dict)
    next_start: float = 0
    model_next: dict = field(default_factory=dict)


def _interval(rule):
    if rule is None:
        return 0.0
    return max(
        60 / rule.requestsPerMinute if rule.requestsPerMinute else 0,
        (rule.minIntervalMs or 0) / 1000,
    )


class RequestPacer:
    def __init__(self, *, max_queue=256, max_wait=60.0, clock=time.monotonic):
        self.states = {}
        self.max_queue, self.max_wait, self.clock = max_queue, max_wait, clock

    @asynccontextmanager
    async def acquire(
        self, channel_id: str, model: str, policy: RequestPacing | None, cancellation
    ):
        if cancellation.is_set():
            raise asyncio.CancelledError
        if not policy or not policy.enabled:
            yield
            return
        state = self.states.setdefault(channel_id, _State())
        ticket = object()
        start = self.clock()
        acquired = False
        model_rule = policy.models.get(model)
        provider_interval = max(_interval(policy), _interval(policy.keys))
        provider_limit = min(
            (
                v
                for v in (
                    policy.maxConcurrentRequests,
                    policy.keys.maxConcurrentRequests if policy.keys else None,
                )
                if v
            ),
            default=0,
        )
        model_limit = model_rule.maxConcurrentRequests if model_rule else None
        async with state.condition:
            if len(state.queue) >= self.max_queue:
                raise PacingDenied("queue_full")
            state.queue.append(ticket)
        try:
            while True:
                if cancellation.is_set():
                    raise asyncio.CancelledError
                async with state.condition:
                    now = self.clock()
                    remaining = self.max_wait - (now - start)
                    if remaining <= 0:
                        raise PacingDenied("queue_expired")
                    delay = max(0, state.next_start - now, state.model_next.get(model, 0) - now)
                    if (
                        state.queue[0] is ticket
                        and not delay
                        and (not provider_limit or state.active < provider_limit)
                        and (not model_limit or state.model_active.get(model, 0) < model_limit)
                    ):
                        state.queue.pop(0)
                        state.active += 1
                        state.model_active[model] = state.model_active.get(model, 0) + 1
                        state.next_start = now + provider_interval
                        state.model_next[model] = now + _interval(model_rule)
                        acquired = True
                        state.condition.notify_all()
                        break
                    try:
                        async with asyncio.timeout(min(remaining, max(delay, 0.1), 0.25)):
                            await state.condition.wait()
                    except TimeoutError:
                        pass
            yield
        finally:
            async with state.condition:
                if not acquired and ticket in state.queue:
                    state.queue.remove(ticket)
                if acquired:
                    state.active -= 1
                    state.model_active[model] -= 1
                    if not state.model_active[model]:
                        del state.model_active[model]
                state.condition.notify_all()
