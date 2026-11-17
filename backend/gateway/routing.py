"""Resolve caller model selectors without network or configuration side effects."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from backend.catalog.identity import resolve_selector
from backend.configuration.models import Channel, Configuration


@dataclass(frozen=True, slots=True)
class Route:
    channel: Channel
    model_id: str


def native_route(config: Configuration, channel_id: str, model_id: str) -> Route:
    channel = next((channel for channel in config.channels if channel.id == channel_id), None)
    if channel is None:
        raise ValueError("Provider not found.")
    if channel.disabled:
        raise ValueError("This provider is disabled.")
    return Route(channel, model_id)


def resolve_route(
    config: Configuration,
    requested_model: str | None,
    discovered: Mapping[str, Iterable[str]],
) -> Route:
    channels = {channel.id: channel for channel in config.channels}
    default = (
        channels.get(config.default_channel_id) if config.default_channel_id is not None else None
    )
    model = (
        requested_model or (default.models.default if default else None) or config.default_model_id
    )
    if not model:
        raise ValueError("Select a model first.")
    if not requested_model and default:
        return native_route(config, default.id, model)
    names = {
        name: channel.id
        for channel in config.channels
        for name in (channel.id, channel.alias)
        if name
    }
    if "/" in model and model.split("/", 1)[0] in names:
        name, model = model.split("/", 1)
        channel_id = names[name]
    else:
        channel_id = config.default_channel_id
    if not channel_id:
        raise ValueError("Use provider/model or select a default provider.")
    channel = native_route(config, channel_id, model).channel
    known = set(discovered.get(channel.id, () if channel.models.live else channel.models.static))
    known.update(custom.modelId for custom in config.models.custom if custom.provider == channel.id)
    known.update(channel.models.aliases)
    if channel.models.default:
        known.add(channel.models.default)
    known.update(channel.models.metadata.capabilities)
    # Visibility is a catalog choice, not permission to explicitly address a model.
    return Route(channel, resolve_selector(config.models, channel, known, model).native_id)
