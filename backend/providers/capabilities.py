"""Destination capabilities, resolved after explicit operator overrides."""

from .registry import match_destination


def native_compaction(channel, model: str, compact: bool) -> bool:
    if channel.protocol_for_model(model) != "openai-responses":
        return False
    policy = channel.request_policy
    override = (
        policy.supportsNativeCompactEndpoint if compact else policy.supportsNativeCompactionTrigger
    )
    if override is not None:
        return override
    match = match_destination(channel)
    return match is not None and match.service.id in {"openai", "chatgpt"}


def native_search(channel, wire: str) -> bool:
    match = match_destination(channel)
    return wire == "responses" and match is not None and match.service.id in {"openai", "chatgpt"}
