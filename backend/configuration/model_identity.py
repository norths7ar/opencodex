"""Persist only the ambiguous legacy spellings, not projected model IDs."""

from collections.abc import Iterable

from .models import Channel, Configuration, GlobalModels


def legacy_slug(native_id: str) -> str:
    return native_id.replace("/", "-")


def _observed_ids(models: GlobalModels, channel: Channel) -> set[str]:
    known_models = (models.discovery or {}).get("knownModels")
    roster = known_models.get(channel.id) if isinstance(known_models, dict) else None
    return {
        native_id
        for field in ("ids", "removed")
        if isinstance(roster, dict) and isinstance(roster.get(field), list)
        for native_id in roster[field]
        if isinstance(native_id, str)
    }


def models_by_legacy_slug(
    models: GlobalModels, channel: Channel, known_ids: Iterable[str]
) -> dict[str, set[str]]:
    declarations = channel.models.metadata
    known = {
        *known_ids,
        *channel.models.static,
        *channel.models.selected,
        *_observed_ids(models, channel),
        *channel.models.aliases,
        *declarations.capabilities,
        *(
            model
            for model, values in declarations.overrides.items()
            if values.model_fields_set & {"displayName", "contextWindow"}
        ),
        *(item.modelId for item in models.custom if item.provider == channel.id),
    }
    if channel.models.default:
        known.add(channel.models.default)
    pairs: dict[str, set[str]] = {}
    for native_id in known:
        pairs.setdefault(legacy_slug(native_id), set()).add(native_id)
    return pairs


def colliding_slugs(
    models: GlobalModels, channel: Channel, known_ids: Iterable[str] = ()
) -> set[str]:
    return {
        slug
        for slug, members in models_by_legacy_slug(models, channel, known_ids).items()
        if len(members) > 1
    } | set(models.legacy_selector_collisions.get(channel.id, ()))


def selector_collisions(config: Configuration) -> dict[str, set[str]]:
    return {
        channel.id: collisions
        for channel in config.channels
        if (collisions := colliding_slugs(config.models, channel))
    }


def remember_selector_collisions(previous: dict[str, set[str]], current: Configuration) -> None:
    history = {}
    for channel in current.channels:
        collisions = colliding_slugs(current.models, channel) | previous.get(channel.id, set())
        if collisions:
            history[channel.id] = sorted(collisions)
    if history != current.models.legacy_selector_collisions:
        current.models = current.models.model_copy(update={"legacy_selector_collisions": history})
