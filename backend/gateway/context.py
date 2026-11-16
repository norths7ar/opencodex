"""Identities and observations for logical requests and physical sends.

These records do not prescribe routing, cancellation, or settlement policy. A
physical attempt represents one send, including an OAuth retry to the same target.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

WireProtocol = Literal[
    "chat/completions",
    "responses",
    "responses/compact",
    "messages",
    "messages/count_tokens",
    "alpha/search",
]


class CancellationSignal(Protocol):
    """A caller-owned signal, shared with work descended from a request."""

    def is_set(self) -> bool: ...

    async def wait(self) -> object: ...


class TerminalStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UsageStatus(StrEnum):
    REPORTED = "reported"
    UNREPORTED = "unreported"
    ESTIMATED = "estimated"


class RequestPurpose(StrEnum):
    GENERATION = "generation"
    COMPACTION = "compaction"
    SEARCH = "search"
    VISION = "vision"
    SIZING = "sizing"
    PROBE = "probe"


class AttemptReason(StrEnum):
    INITIAL = "initial"
    AUTH_REFRESH = "auth_refresh"
    FALLBACK = "fallback"
    CONTINUATION = "continuation"


@dataclass(frozen=True, slots=True)
class Usage:
    """Observed normalized usage; absent usage is not the same as zero usage."""

    status: UsageStatus
    values: dict[str, int | float] | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    """Response and terminal facts, independent of whether usage was reported."""

    status_code: int | None = None
    terminal_status: TerminalStatus | None = None
    error_code: str | None = None
    cancelled: bool = False
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class RequestContext:
    """One logical caller request, including any retries and fallback sends."""

    request_id: str
    protocol: WireProtocol
    requested_model: str
    streaming: bool
    cancellation: CancellationSignal
    purpose: RequestPurpose = RequestPurpose.GENERATION
    client_id: str | None = None
    parent_request_id: str | None = None
    workflow_id: str | None = None
    conversation_id: str | None = None
    requested_service_tier: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """One physical send and its actual target, not the caller's model alias."""

    request_id: str
    attempt_id: str
    channel_id: str
    model: str
    protocol: WireProtocol
    cancellation: CancellationSignal
    reason: AttemptReason = AttemptReason.INITIAL
    # Wall-clock send time selects time-dependent prices; elapsed time is measured separately.
    sent_at: datetime | None = None
    elapsed_ms: float | None = None
    account_identity: str | None = None
    output_started: bool = False
    outcome: Outcome | None = None
    usage: Usage | None = None
    served_model: str | None = None
    response_service_tier: str | None = None
