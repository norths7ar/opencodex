"""Original root-workflow count admission and bounded ten-minute observations.

Counts are process-local, as in lib/workflow-budget.ts. This module does not
implement the separate durable token spend-reservation ledger.
"""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import cast

BOUNDS = {
    "maxConcurrentChildren": {"min": 2, "max": 64},
    "maxPhysicalSends": {"min": 1, "max": 10_000},
    "maxDistinctChildren": {"min": 1, "max": 1024},
}
DEFAULTS = {
    "maxConcurrentChildren": 8,
    "maxPhysicalSends": 256,
    "maxDistinctChildren": 64,
}
WINDOW_MS = 600_000
WINDOW_SLOTS = 12
INTERACTIVE_RESERVE = 1
MAX_ROOTS = 512
EVENT_CAPACITY = 64
LOCAL_REFUSAL_HEADER = "x-opencodex-local-refusal"


def validate_settings(value):
    if not isinstance(value, dict) or value.keys() - BOUNDS.keys():
        raise ValueError("workflowBudget accepts only the three count ceilings.")
    for key, number in value.items():
        bounds = BOUNDS[key]
        if type(number) is not int or not bounds["min"] <= number <= bounds["max"]:
            raise ValueError(f"{key} must be an integer from {bounds['min']} to {bounds['max']}.")
    return value


def policy(config=None):
    raw = config.features.request.workflow_budget if config is not None else None
    try:
        overrides = validate_settings(raw)
    except ValueError:
        overrides = {}
    return {**DEFAULTS, **overrides}


class WorkflowDenied(RuntimeError):
    status = 429

    def __init__(self, reason, root_id):
        self.reason = reason
        self.root_id = root_id
        self.code = reason.replace("-", "_")
        messages = {
            "workflow-sends-exhausted": "The task reached its send ceiling for the current window. The window rolls forward automatically.",
            "workflow-children-exhausted": "The task reached its distinct child-thread ceiling for the current window. Quiet children age out of the count.",
            "workflow-concurrency-exhausted": "The task has no free concurrency slot. Slots are released as in-flight turns finish.",
            "workflow-tracking-exhausted": "The gateway is already tracking its maximum number of tasks, all with active work or unexpired counts.",
        }
        super().__init__(
            f"This proxy refused the request locally before this send contacted a provider: {messages[reason]}"
        )


@dataclass
class _Root:
    last_seen: float
    active: int = 0
    lifetime_sends: int = 0
    send_slots: list = field(default_factory=lambda: [None] * WINDOW_SLOTS)
    send_counts: list = field(default_factory=lambda: [0] * WINDOW_SLOTS)
    children: dict = field(default_factory=dict)

    def sends(self, now):
        oldest = math.floor(now / (WINDOW_MS // WINDOW_SLOTS)) - (WINDOW_SLOTS - 1)
        return sum(
            count
            for at, count in zip(self.send_slots, self.send_counts, strict=False)
            if at is not None and at >= oldest
        )

    def count_children(self, now):
        cutoff = now - WINDOW_MS
        self.children = {key: at for key, at in self.children.items() if at > cutoff}
        return len(self.children)

    def charge(self, now):
        slot = math.floor(now / (WINDOW_MS // WINDOW_SLOTS))
        index = slot % WINDOW_SLOTS
        if self.send_slots[index] != slot:
            self.send_slots[index], self.send_counts[index] = slot, 0
        self.send_counts[index] += 1
        self.lifetime_sends += 1
        self.last_seen = now


class WorkflowLease:
    """One admitted request; release after the full stream ends, including cancellation."""

    def __init__(self, ledger, root_id):
        self.ledger = ledger
        self.root_id = root_id
        self.released = False

    def reserve_send(self):
        self.ledger.reserve_send(self.root_id)

    def release(self):
        with self.ledger.lock:
            if self.released:
                return
            self.released = True
            root = self.ledger.roots.get(self.root_id)
            if root is not None:
                root.active = max(0, root.active - 1)
                root.last_seen = self.ledger.clock()


class WorkflowBudget:
    def __init__(self, config=None, *, clock=None):
        self.config = config or (lambda: None)
        self.clock = clock or (lambda: time.time() * 1000)
        self.roots = {}
        self.events = deque(maxlen=EVENT_CAPACITY)
        # Management sync handlers and the asynchronous dispatch path share this instance.
        self.lock = threading.RLock()

    def settings(self):
        return {
            **policy(self.config()),
            "interactiveReserve": INTERACTIVE_RESERVE,
            "windowMs": WINDOW_MS,
            "defaults": dict(DEFAULTS),
            "bounds": {key: dict(value) for key, value in BOUNDS.items()},
        }

    def _refuse(self, root_id, reason, now):
        root = self.roots.get(root_id)
        self.events.append(
            {
                "at": now,
                "kind": "refused",
                "rootId": root_id,
                "reason": reason,
                "sends": root.sends(now) if root else 0,
                "children": root.count_children(now) if root else 0,
            }
        )
        raise WorkflowDenied(reason, root_id)

    def admit_http(self, headers):
        root = (headers.get("x-codex-parent-thread-id") or "").strip()
        child = (headers.get("thread-id") or "").strip() or None
        return self.admit(
            root or None, "worker" if root and child and child != root else "interactive", child
        )

    def admit(self, root_id, lane="interactive", child_id=None):
        if not root_id:
            return None
        with self.lock:
            now = self.clock()
            limits = policy(self.config())
            root = self.roots.get(root_id)
            if root is None:
                if len(self.roots) >= MAX_ROOTS:
                    eligible = [
                        (key, value)
                        for key, value in self.roots.items()
                        if not value.active
                        and not value.sends(now)
                        and not value.count_children(now)
                    ]
                    if not eligible:
                        self._refuse(root_id, "workflow-tracking-exhausted", now)
                    del self.roots[min(eligible, key=lambda pair: pair[1].last_seen)[0]]
                root = self.roots[root_id] = _Root(now)
            root.last_seen = now
            if root.sends(now) >= limits["maxPhysicalSends"]:
                self._refuse(root_id, "workflow-sends-exhausted", now)
            # Match the original order: an already known child can refresh its admission.
            if (
                child_id is not None
                and child_id not in root.children
                and root.count_children(now) >= limits["maxDistinctChildren"]
            ):
                self._refuse(root_id, "workflow-children-exhausted", now)
            ceiling = limits["maxConcurrentChildren"] - (
                INTERACTIVE_RESERVE if lane == "worker" else 0
            )
            if root.active >= ceiling:
                self._refuse(root_id, "workflow-concurrency-exhausted", now)
            root.active += 1
            if child_id is not None:
                root.children[child_id] = now
            return WorkflowLease(self, root_id)

    def reserve_send(self, root_id):
        """Atomically charge immediately before each physical send, including retries."""
        if isinstance(root_id, WorkflowLease):
            root_id = root_id.root_id
        if not root_id:
            return
        with self.lock:
            root = self.roots.get(root_id)
            if root is None:
                return
            now = self.clock()
            if root.sends(now) >= policy(self.config())["maxPhysicalSends"]:
                self._refuse(root_id, "workflow-sends-exhausted", now)
            root.charge(now)

    def _snapshot(self, root_id, now, limits):
        root = self.roots.get(root_id)
        if root is None:
            return None
        return {
            "active": root.active,
            "sends": root.sends(now),
            "children": root.count_children(now),
            "lifetimeSends": root.lifetime_sends,
            "windowMs": WINDOW_MS,
            "maxPhysicalSends": limits["maxPhysicalSends"],
            "maxDistinctChildren": limits["maxDistinctChildren"],
        }

    def view(self, root_id=None, limit=20):
        with self.lock:
            now, limits = self.clock(), policy(self.config())
            events = list(reversed(self.events))
            if root_id is not None:
                snapshot = self._snapshot(root_id, now, limits)
                return {
                    "root": {"rootId": root_id, **snapshot} if snapshot is not None else None,
                    "events": [event.copy() for event in events if event["rootId"] == root_id][
                        :limit
                    ],
                }
            roots = sorted(self.roots, key=lambda key: self.roots[key].last_seen, reverse=True)[
                :limit
            ]
            return {
                "roots": [
                    {"rootId": key, **cast(dict[str, int], self._snapshot(key, now, limits))}
                    for key in roots
                ],
                "events": [event.copy() for event in events[:limit]],
            }

    def clear(self, root_id):
        with self.lock:
            now = self.clock()
            before = self._snapshot(root_id, now, policy(self.config()))
            if before is None:
                return None
            root = self.roots[root_id]
            root.send_slots = [None] * WINDOW_SLOTS
            root.send_counts = [0] * WINDOW_SLOTS
            root.children.clear()
            root.last_seen = now
            self.events.append(
                {
                    "at": now,
                    "kind": "cleared",
                    "rootId": root_id,
                    "sends": before["sends"],
                    "children": before["children"],
                }
            )
            return before
