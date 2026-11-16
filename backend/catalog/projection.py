"""Public model rows shared by policy consumers and client-specific projections."""

from backend.catalog import EffectiveModel


def model_rows(models: tuple[EffectiveModel, ...]) -> list[dict]:
    rows = []
    for model in models:
        values = dict(model.metadata.values)
        values.update(
            provider=model.identity.channel_id,
            id=model.identity.native_id,
            namespaced=model.routed_selector,
            displayName=model.display_name,
            selected=model.selected,
            disabled=model.disabled,
        )
        if model.context_window:
            values["contextWindow"] = model.context_window
        rows.append(values)
    return rows
