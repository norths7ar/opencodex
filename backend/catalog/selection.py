"""Reconcile selection policy from a complete roster without writing configuration."""

from collections.abc import Iterable
from datetime import UTC, datetime

from backend.configuration.models import Configuration

from .identity import ModelIdentity


def reconcile_discovery(
    config: Configuration,
    channel_id: str,
    ids: Iterable[str],
    *,
    observed_at: datetime | None = None,
) -> Configuration:
    """Return a candidate; the caller must recheck and persist it transactionally."""
    channel = next((channel for channel in config.channels if channel.id == channel_id), None)
    if channel is None:
        return config
    discovered = sorted(set(ids))
    metadata = channel.models.metadata
    global_updates = {}
    config_updates = {}

    def disable(models: Iterable[str]) -> None:
        selectors = [ModelIdentity(channel.id, model).routed_selector for model in models]
        disabled = global_updates.get("disabled", config.models.disabled)
        global_updates["disabled"] = list(dict.fromkeys([*disabled, *selectors]))

    initial = metadata.initial_selection
    if initial and initial.get("status") == "pending":
        all_off = len(discovered) >= 20
        if all_off:
            disable(discovered)
        initial_selection = {
            **initial,
            "status": "all-off" if all_off else "ready",
            "modelCount": len(discovered),
        }
        updated = channel.model_copy(
            update={
                "models": channel.models.model_copy(
                    update={
                        "metadata": metadata.model_copy(
                            update={"initial_selection": initial_selection}
                        ),
                    }
                ),
            }
        )
        config_updates["channels"] = [
            updated if entry.id == channel_id else entry for entry in config.channels
        ]
    if not channel.models.live:
        if global_updates:
            config_updates["models"] = config.models.model_copy(update=global_updates)
        return config.model_copy(update=config_updates).model_copy(deep=True)

    discovery = dict(config.models.discovery or {})
    known = dict(discovery.get("knownModels", {}))
    prior = known.get(channel.id)
    timestamp = (observed_at or datetime.now(UTC)).isoformat()
    if prior is None:
        known[channel.id] = {"ids": discovered, "removed": [], "updatedAt": timestamp}
    else:
        active, removed, seen = set(prior["ids"]), set(prior.get("removed", [])), set(discovered)
        arrivals = sorted(seen - active)
        current = {"ids": discovered, "removed": sorted((removed | (active - seen)) - seen)}
        previous = {key: value for key, value in prior.items() if key != "updatedAt"}
        known[channel.id] = {
            **current,
            "updatedAt": prior.get("updatedAt", timestamp) if current == previous else timestamp,
        }
        policy = metadata.new_model_policy
        if policy not in {"on", "off"}:
            policy = discovery.get("newModelPolicy", "on")
        if policy == "off" and not channel.models.selected:
            disable(arrivals)
        if arrivals:
            recent = dict(discovery.get("recentArrivals", {}))
            recent[channel.id] = [
                *recent.get(channel.id, []),
                *[{"id": model, "at": timestamp} for model in arrivals],
            ][-50:]
            discovery["recentArrivals"] = recent
    discovery["knownModels"] = known
    global_updates["discovery"] = discovery
    config_updates["models"] = config.models.model_copy(update=global_updates)
    return config.model_copy(update=config_updates).model_copy(deep=True)
