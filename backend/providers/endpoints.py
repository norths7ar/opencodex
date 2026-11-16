"""Pure generation and model-discovery endpoint resolution."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from backend.configuration.models import Channel, Protocol

from .registry import match_destination


@dataclass(frozen=True, slots=True)
class GenerationEndpoint:
    url: str
    protocol: Protocol


def wire_protocol(channel: Channel, model_id: str) -> Protocol:
    """Use an exact model override or the channel's explicit wire protocol."""
    return channel.protocol_for_model(model_id)


def _override_url(base: str, suffix: str) -> str:
    if not suffix.startswith("/"):
        raise ValueError("Endpoint overrides must be base URL suffixes beginning with '/'.")
    return base + suffix


def _fallback_generation(base: str, protocol: Protocol) -> str:
    if protocol == "openai-chat":
        return base.removesuffix("/chat/completions") + "/chat/completions"
    if protocol == "openai-responses":
        return base.removesuffix("/responses").removesuffix("/v1") + "/v1/responses"
    return re.sub(r"/(?:v1/messages|v1)$", "", base, flags=re.IGNORECASE) + "/v1/messages"


def generation_endpoint(
    channel: Channel, model_id: str, *, compact: bool = False
) -> GenerationEndpoint:
    """Resolve from explicit suffix, matched service, then legacy general API defaults."""

    protocol = wire_protocol(channel, model_id)
    if compact and protocol != "openai-responses":
        raise ValueError("Native compaction requires the Responses protocol.")
    field = {
        "openai-chat": "chat",
        "openai-responses": "responses",
        "anthropic": "messages",
    }[protocol]
    base = channel.endpoints.base_url
    suffix = getattr(channel.endpoints, field)
    if suffix:
        url = _override_url(base, suffix)
    else:
        match = match_destination(channel)
        declared = getattr(match.destination, field) if match else None
        url = declared or _fallback_generation(base, protocol)
    if compact:
        parsed = urlsplit(url)
        url = parsed._replace(path=parsed.path + "/compact").geturl()
    return GenerationEndpoint(url, protocol)


def discovery_url(channel: Channel) -> str:
    """Resolve the model inventory independently of the generation path."""

    base = channel.endpoints.base_url
    if channel.endpoints.discovery:
        return _override_url(base, channel.endpoints.discovery)
    match = match_destination(channel)
    if match and match.destination.discovery:
        return match.destination.discovery
    if channel.protocol == "anthropic" and not urlsplit(base).path:
        base += "/v1"
    return base + "/models"
