"""Resolve caller model selectors without network or configuration side effects."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from backend.catalog.identity import resolve_selector
from backend.configuration.models import Channel, Configuration


@dataclass(frozen=True, slots=True)
class Route:
    channel: Channel
    model_id: str


def resolve_route(
    config: Configuration,
    requested_model: str | None,
    discovered: Mapping[str, Iterable[str]],
) -> Route:
    channels = {channel.id: channel for channel in config.channels}
    default = channels.get(config.default_channel_id)
    model = (
        requested_model or (default.models.default if default else None) or config.default_model_id
    )
    if not model:
        raise ValueError("Select a model first.")
    names = {
        name: channel.id
        for channel in config.channels
        for name in (channel.id, channel.alias)
        if name
    }
    if "/" in model and model.split("/", 1)[0] in names:
        name, model = model.split("/", 1)
        channel_id = names[name]
    elif "/" not in model and (
        model.startswith(("gpt-", "o1-", "o3-", "o4-")) or model == "codex-auto-review"
    ):
        channel_id = "openai"
        if channel_id not in channels or channels[channel_id].disabled:
            raise ValueError(f"Model {model} requires the enabled openai channel.")
    else:
        channel_id = config.default_channel_id
    if not channel_id:
        raise ValueError("Use provider/model or select a default provider.")
    channel = channels.get(channel_id)
    if channel is None:
        raise ValueError("Provider not found.")
    if channel.disabled:
        raise ValueError("This provider is disabled.")
    known = set(discovered.get(channel.id, () if channel.models.live else channel.models.static))
    known.update(custom.modelId for custom in config.models.custom if custom.provider == channel.id)
    known.update(channel.models.aliases)
    if channel.models.default:
        known.add(channel.models.default)
    known.update(channel.models.metadata.capabilities)
    # Visibility is a catalog choice, not permission to explicitly address a model.
    return Route(channel, resolve_selector(config.models, channel, known, model).native_id)
