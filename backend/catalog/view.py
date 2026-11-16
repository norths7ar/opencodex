"""Read-only composition of user settings, discovery and reference metadata."""

from collections.abc import Callable
from dataclasses import dataclass

from backend.configuration.models import Channel, Configuration, ModelMetadata, Protocol

from .directory import DirectorySnapshot
from .identity import ModelAlias, ModelIdentity, effective_aliases
from .metadata import ResolvedMetadata, resolve_metadata


@dataclass(frozen=True, slots=True)
class EffectiveModel:
    identity: ModelIdentity
    protocol: Protocol
    metadata: ResolvedMetadata
    alias: ModelAlias | None
    selected: bool
    disabled: bool
    subscription: bool
    initial_selection_pending: bool
    custom_id: str | None
    context_cap: int | None

    @property
    def routed_selector(self) -> str:
        return self.identity.routed_selector

    @property
    def visible(self) -> bool:
        return self.selected and not self.disabled

    @property
    def display_name(self) -> str:
        return self.metadata.values.get("displayName") or self.identity.native_id

    @property
    def context_window(self) -> int | None:
        declared = self.metadata.values.get("contextWindow")
        if not self.context_cap:
            return declared
        return min(declared, self.context_cap) if declared else self.context_cap

    @property
    def context_capped(self) -> bool:
        declared = self.metadata.values.get("contextWindow")
        return bool(self.context_cap and declared and declared > self.context_cap)


def effective_models(
    config: Configuration,
    directory_snapshot_lookup: Callable[[Channel], DirectorySnapshot | None],
    *,
    preset_lookup: Callable[[Channel, str], ModelMetadata | None] | None = None,
    official_lookup: Callable[[Channel], dict[str, dict]] | None = None,
) -> tuple[EffectiveModel, ...]:
    """Build the effective view from cached data only; lookups must not refresh it."""
    disabled = set(config.models.disabled)
    custom_by_channel = {}
    for custom in config.models.custom:
        custom_by_channel.setdefault(custom.provider, {})[custom.modelId] = custom

    result = []
    for channel in config.channels:
        snapshot = (
            directory_snapshot_lookup(channel)
            if channel.models.live and not channel.disabled
            else None
        )
        upstream = {row["id"]: row for row in snapshot.rows} if snapshot else {}
        custom_models = custom_by_channel.get(channel.id, {})
        known = dict.fromkeys(
            [*(upstream if channel.models.live else channel.models.static), *custom_models]
        )
        aliases = effective_aliases(config.models, channel, known)
        selected = set(channel.models.selected)
        pending = (channel.models.metadata.initial_selection or {}).get("status") == "pending"
        cap = (config.models.context_caps or {}).get(channel.id)
        official = official_lookup(channel) if official_lookup else {}
        provenance = (
            {"updatedAt": snapshot.last_success_at, "stale": snapshot.status == "failed"}
            if snapshot
            else None
        )
        for native_id in known:
            identity = ModelIdentity(channel.id, native_id)
            custom = custom_models.get(native_id)
            result.append(
                EffectiveModel(
                    identity=identity,
                    protocol=channel.protocol_for_model(native_id),
                    metadata=resolve_metadata(
                        channel,
                        native_id,
                        upstream=upstream.get(native_id),
                        upstream_provenance=provenance,
                        preset=preset_lookup(channel, native_id) if preset_lookup else None,
                        official=official.get(native_id),
                        custom=custom,
                    ),
                    alias=aliases.get(native_id),
                    selected=not selected or native_id in selected,
                    disabled=channel.disabled
                    or pending
                    or (identity.namespaced_id in disabled or identity.routed_selector in disabled),
                    subscription=channel.auth_mode == "oauth",
                    initial_selection_pending=pending,
                    custom_id=custom.id if custom else None,
                    context_cap=cap,
                )
            )
    return tuple(result)
