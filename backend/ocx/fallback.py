"""Explicit ordered fallback routes, separate from the retired Combo/pool policies."""

import copy
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .compaction import decode_summary


def split_selector(value: str) -> tuple[str, str]:
    provider, separator, model = value.partition("/")
    if not separator or not provider or not model.strip() or value != value.strip():
        raise ValueError("Use a provider/model selector for each fallback route.")
    return provider, model


class FallbackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    routes: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("routes")
    @classmethod
    def route_selectors(cls, routes):
        for source, targets in routes.items():
            split_selector(source)
            if not targets:
                raise ValueError("A fallback route needs at least one backup model.")
            if len(targets) != len(set(targets)) or source in targets:
                raise ValueError("Backup models must be distinct and different from the source.")
            for target in targets:
                split_selector(target)
        return routes


@dataclass(frozen=True)
class FallbackTarget:
    provider: str
    model: str

    @property
    def selector(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class FallbackPlan:
    source: FallbackTarget
    candidates: tuple[FallbackTarget, ...]
    skipped: tuple[dict, ...] = ()


def failure_trigger(
    status: int | None = None,
    *,
    exception: BaseException | None = None,
    response_started: bool = False,
    upstream_failure: bool = True,
    error_code: str | None = None,
) -> str | None:
    """Only pre-delivery transient failures open this user-selected chain.

    Local validation/authentication errors and successful streams with later error
    events never trigger another request. Callers identify local errors explicitly.
    """
    if response_started or not upstream_failure:
        return None
    if error_code in {
        "upstream_no_response",
        "upstream_closed_before_response",
        "upstream_reset_replay_refused",
        "origin_rejected",
        "cyber_policy",
        "context_length_exceeded",
        "invalid_request_error",
    }:
        return None
    if isinstance(exception, httpx.ConnectError | httpx.ConnectTimeout):
        return "connection_error"
    if exception is not None:
        # A read/write timeout or reset can happen after the POST was accepted.
        return None
    if status == 429:
        return "rate_limited"
    if status in {401, 402, 403, 404, 408, 425}:
        return "upstream_unavailable"
    if error_code in {
        "model_not_found",
        "model_unavailable",
        "unsupported_model",
        "unsupported_input_modality",
        "free_rate_limited",
        "permission_denied",
        "subscription_required",
        "invalid_api_key",
        "insufficient_quota",
        "usage_limit_exceeded",
        "usage_limit_reached",
        "1308",
        "payment_required",
        "billing_error",
        "insufficient_balance",
        "rate_limit_exceeded",
        "server_is_overloaded",
        "upstream_server_error",
    }:
        return "target_unavailable"
    if status is not None and 500 <= status <= 599:
        return "upstream_5xx"
    return None


class ReplayUnavailable(ValueError):
    """The selected backup cannot receive this request without losing provider state."""


def upstream_error_code(content: bytes) -> str | None:
    """Read original refusal codes before telemetry deliberately coarsens them."""
    try:
        value = json.loads(content)
    except ValueError, RecursionError:
        return None
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if isinstance(message, str) and "origin_rejected" in message.lower():
        return "origin_rejected"
    code = error.get("code") or error.get("type")
    return code if isinstance(code, str) else None


def _portable_items(items, *, cross_provider: bool):
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "item_reference":
            raise ReplayUnavailable("The request contains an upstream-only item reference.")
        if (
            kind in {"compaction", "compaction_summary", "context_compaction"}
            and item.get("encrypted_content")
            and decode_summary(item["encrypted_content"]) is None
        ):
            raise ReplayUnavailable("The request contains an opaque compaction checkpoint.")
        if cross_provider:
            source = item.get("source")
            if (
                item.get("file_id")
                or item.get("file_ids")
                or (
                    isinstance(source, dict)
                    and (source.get("file_id") or source.get("type") == "file")
                )
            ):
                raise ReplayUnavailable("Uploaded files belong to the original provider.")
            if kind in {"thinking", "redacted_thinking"} and (
                item.get("signature") or item.get("data")
            ):
                raise ReplayUnavailable("Signed thinking cannot be replayed to another provider.")
        if kind == "reasoning":
            # Account/model-bound ciphertext is not portable; preserve any readable summary.
            if (
                item.get("encrypted_content")
                and not item.get("summary")
                and not item.get("content")
            ):
                raise ReplayUnavailable("The request contains reasoning without a readable replay.")
            item.pop("encrypted_content", None)
            item.pop("id", None)
            item.pop("status", None)
        for key in ("content", "output"):
            _portable_items(item.get(key), cross_provider=cross_provider)


def prepare_retry(
    body: dict,
    source: FallbackTarget,
    target: FallbackTarget,
    *,
    expanded_body: dict | None = None,
) -> dict:
    """Build a backup request from the original request, never a prior mutated attempt.

    The caller resolves previous_response_id against the *source* provider/model
    before supplying expanded_body. Unknown/native-only history cannot be guessed.
    """
    if body.get("previous_response_id") and expanded_body is None:
        raise ReplayUnavailable("Resolve the original response history before changing its route.")
    value = copy.deepcopy(expanded_body if expanded_body is not None else body)
    if value.get("conversation"):
        raise ReplayUnavailable("The request uses an upstream-managed conversation.")
    _portable_items(value.get("input"), cross_provider=source.provider != target.provider)
    _portable_items(value.get("messages"), cross_provider=source.provider != target.provider)
    value.pop("previous_response_id", None)
    value["model"] = target.selector
    metadata = value.get("client_metadata")
    if isinstance(metadata, dict):
        metadata.pop("x-codex-turn-state", None)
    return value


def retry_headers(headers) -> dict:
    """Keep conversation identity while dropping transport and source-account state."""
    removed = {
        "authorization",
        "x-api-key",
        "chatgpt-account-id",
        "x-codex-turn-state",
        "content-length",
        "content-encoding",
    }
    return {key: value for key, value in headers.items() if key.lower() not in removed}


class SimpleFallback:
    def __init__(self, store):
        self.store = store
        self._events = deque(maxlen=20)
        self._event_lock = Lock()

    def settings(self, config=None) -> FallbackSettings:
        config = config or self.store.load()
        return FallbackSettings.model_validate(getattr(config, "simpleFallback", None) or {})

    def save(self, value: FallbackSettings):
        with self.store.mutation_lock:
            config = self.store.load()
            providers = {provider.id for provider in config.providers}
            for source, targets in value.routes.items():
                for selector in [source, *targets]:
                    if split_selector(selector)[0] not in providers:
                        raise ValueError(
                            f"Fallback provider does not exist: {split_selector(selector)[0]}"
                        )
            self.store.save(config.model_copy(update={"simpleFallback": value.model_dump()}))
        return self.state()

    def plan(self, provider: str, model: str, *, requested: str | None = None) -> FallbackPlan:
        config = self.store.load()
        settings = self.settings(config)
        source = FallbackTarget(provider, model)
        if not settings.enabled:
            return FallbackPlan(source, ())
        selectors = settings.routes.get(requested) if requested else None
        if selectors is None:
            selectors = settings.routes.get(source.selector, [])
        selected = []
        skipped = []
        seen = {source}
        for selector in selectors:
            try:
                selected_provider, _ = split_selector(selector)
                if not any(p.id == selected_provider for p in config.providers):
                    raise ValueError("The configured fallback provider no longer exists.")
                resolved_provider, resolved_model = self.store.route(selector)
                target = FallbackTarget(resolved_provider.id, resolved_model)
                if target not in seen:
                    selected.append(target)
                    seen.add(target)
            except (ValueError, KeyError) as exc:
                skipped.append({"target": selector, "reason": str(exc)})
        return FallbackPlan(source, tuple(selected), tuple(skipped))

    def record(
        self,
        source: FallbackTarget,
        previous: FallbackTarget,
        target: FallbackTarget,
        trigger: str,
        *,
        outcome: str = "taken",
        reason: str | None = None,
    ):
        event = {
            "at": datetime.now(UTC).isoformat(),
            "route": source.selector,
            "from": previous.selector,
            "to": target.selector,
            "trigger": trigger,
            "outcome": outcome,
        }
        if reason:
            event["reason"] = reason
        with self._event_lock:
            self._events.append(event)

    def state(self) -> dict:
        with self._event_lock:
            events = list(self._events)
        return {
            **self.settings().model_dump(),
            "last": events[-1] if events else None,
            "events": events,
        }
