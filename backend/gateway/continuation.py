"""Account-bound response replay and opaque turn-state projection."""

import copy
from dataclasses import dataclass

from .history import (
    TURN_STATE_HEADER,
    HistoryStore,
    TurnStateStore,
    scrub_subscription_replay,
    subscription_issuer,
)


@dataclass(slots=True)
class Continuation:
    body: dict
    headers: dict[str, str]
    issuer: str | None
    account_changed: bool


class Continuations:
    def __init__(self, history: HistoryStore, turns: TurnStateStore):
        self.history = history
        self.turns = turns

    def prepare(self, channel_id, model, route, body, incoming, subscription=None):
        body = copy.deepcopy(body)
        headers = {key.lower(): value for key, value in incoming.items()}
        issuer = subscription_issuer(subscription["accountId"]) if subscription else None
        changed = False
        if route.inbound == "responses" and (route.translated or route.stateless):
            body, changed = self.history.expand_history(body, channel_id, model, issuer)
        if route.chatgpt and issuer:
            if self.turns.issuer_changed(issuer, channel_id, headers):
                if isinstance(body.get("input"), str):
                    body["input"] = [{"role": "user", "content": body["input"]}]
                body.setdefault("input", [])
                body = scrub_subscription_replay(body)
                changed = True
            raw = headers.pop(TURN_STATE_HEADER, None)
            resolved = (
                None if changed else self.turns.resolve(raw, issuer, channel_id, model, headers)
            )
            if resolved:
                headers[TURN_STATE_HEADER] = resolved
            metadata = body.get("client_metadata")
            if isinstance(metadata, dict) and TURN_STATE_HEADER in metadata:
                raw = metadata.pop(TURN_STATE_HEADER)
                resolved = (
                    None if changed else self.turns.resolve(raw, issuer, channel_id, model, headers)
                )
                if resolved:
                    metadata[TURN_STATE_HEADER] = resolved
        return Continuation(body, headers, issuer, changed)

    def response_headers(self, continuation, channel_id, model, upstream_headers):
        headers = {
            key: upstream_headers[key]
            for key in ("content-type", "retry-after", "x-request-id", "request-id")
            if key in upstream_headers
        }
        if continuation.issuer:
            handle = self.turns.issue(
                upstream_headers.get(TURN_STATE_HEADER),
                continuation.issuer,
                channel_id,
                model,
                continuation.headers,
            )
            if handle:
                headers[TURN_STATE_HEADER] = handle
        return headers

    def remember(
        self,
        continuation,
        channel_id,
        model,
        result,
        *,
        fallback_origin=None,
        persist=True,
        observe=True,
    ):
        if not isinstance(result, dict) or result.get("error") or result.get("status") == "failed":
            return
        if persist:
            self.history.save_response(
                channel_id,
                model,
                continuation.body,
                result,
                continuation.issuer,
                fallback_origin=fallback_origin,
            )
        elif fallback_origin:
            self.history.bind_fallback_response(
                channel_id, model, continuation.body, result, fallback_origin
            )
        if observe and continuation.issuer:
            self.turns.observe_issuer(continuation.issuer, channel_id, continuation.headers)
