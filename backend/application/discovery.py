"""Public discovery state built from a cached snapshot, without upstream I/O."""

from .contracts import ApiError, DiscoveryState


def discovery_state(channel, snapshot, config):
    known = {row["id"] for row in snapshot.rows} if snapshot and snapshot.last_success_at else None
    if known is not None:
        known.update(
            model.modelId for model in config.models.custom if model.provider == channel.id
        )
    default = channel.models.default or (
        config.default_model_id if config.default_channel_id == channel.id else None
    )
    state = (
        "unsupported"
        if channel.disabled or not channel.models.live
        else "not_checked"
        if snapshot is None
        else "failed"
        if snapshot.status == "failed"
        else "ready"
    )
    return DiscoveryState(
        state=state,
        stale_default_model=default if known is not None and default not in known else None,
        stale_aliases={
            model: alias for model, alias in channel.models.aliases.items() if model not in known
        }
        if known is not None
        else {},
        last_success_at=snapshot.last_success_at if snapshot else None,
        last_attempt_at=snapshot.checked_at if snapshot else None,
        error=ApiError(
            code="catalog_refresh_failed", message="Model discovery failed.", retryable=True
        )
        if state == "failed"
        else None,
    )
