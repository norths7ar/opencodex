"""Codex's native model selector compatibility at the client boundary."""

import re

from backend.configuration.models import Configuration
from backend.providers.registry import match_destination

_NATIVE_MODEL = re.compile(r"^(?:gpt-|chatgpt-|o[134](?:-|$)|codex-auto-review$)", re.IGNORECASE)
_CODEX_CLIENTS = {"codex", "codex_cli", "codex_desktop", "codex_ide"}


def resolve_client_selector(
    config: Configuration, selector: str | None, client_id: str | None, protocol: str
) -> str | None:
    if (
        client_id not in _CODEX_CLIENTS
        or protocol not in {"responses", "responses/compact"}
        or not selector
        or "/" in selector
        or not _NATIVE_MODEL.match(selector)
    ):
        return selector
    # User-authored model aliases are deliberate routes, even when named like GPT models.
    if any(
        channel.id == config.default_channel_id
        and not channel.disabled
        and selector.lower() in {alias.lower() for alias in channel.models.aliases.values()}
        for channel in config.channels
    ):
        return selector
    enabled = [channel for channel in config.channels if not channel.disabled]
    subscriptions = [
        channel
        for channel in enabled
        if channel.service_id == "chatgpt" and channel.auth_mode == "oauth"
    ]
    if subscriptions:
        preferred = next(
            (channel for channel in subscriptions if channel.id == config.default_channel_id),
            None,
        )
        if preferred is None and len(subscriptions) == 1:
            preferred = subscriptions[0]
        if preferred is None:
            raise ValueError("Select a default ChatGPT channel or use an explicit channel/model.")
        return f"{preferred.id}/{selector}"
    candidates = [
        channel
        for channel in enabled
        if (destination := match_destination(channel)) is not None
        and destination.service.id == "openai"
    ]
    preferred = next(
        (channel for channel in candidates if channel.id == config.default_channel_id), None
    ) or next((channel for channel in candidates if channel.id == "openai"), None)
    if preferred is None and len(candidates) == 1:
        preferred = candidates[0]
    if preferred is not None:
        return f"{preferred.id}/{selector}"
    if candidates:
        raise ValueError("Select a default OpenAI channel or use an explicit channel/model.")
    if protocol == "responses/compact":
        return selector
    raise ValueError("Codex native models require an enabled ChatGPT or OpenAI channel.")
