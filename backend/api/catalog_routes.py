"""Snapshot-based catalog reads and transactional model edits."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from backend.catalog.identity import ModelIdentity, effective_aliases
from backend.catalog.refresh import CatalogRefresh
from backend.catalog.view import effective_models
from backend.configuration.models import CustomModel, ModelMetadata
from backend.configuration.store import ConfigurationStore
from backend.providers.channels import ChannelService
from backend.telemetry.pricing import _user_rates

from .channel_routes import discovery_view
from .contracts import ModelOverrideUpdate


def model_row(model, channel):
    metadata = model.metadata
    native = model.identity.native_id
    cost = channel.models.metadata.costs.get(native)
    return {
        **metadata.values,
        "provider": channel.id,
        "id": native,
        "namespaced": model.routed_selector,
        "adapter": model.protocol,
        "disabled": model.disabled,
        "selected": model.selected,
        "subscription": model.subscription,
        "displayName": model.display_name,
        "displayNameSource": metadata.sources.get("displayName", {}).get("source", "fallback"),
        "metadataSources": metadata.sources,
        "metadataOverrides": {**metadata.overrides, **({"effectiveCost": cost} if cost else {})},
        "inheritedMetadata": metadata.inherited,
        **({"costOverride": cost, "effectiveCost": cost} if cost else {}),
        **({"contextWindow": model.context_window} if model.context_window is not None else {}),
        **(
            {"contextCap": model.context_cap, "contextCapped": model.context_capped}
            if model.context_cap
            else {}
        ),
        **({"initialSelectionPending": True} if model.initial_selection_pending else {}),
        **({"custom": True, "customId": model.custom_id} if model.custom_id else {}),
        **(
            {"displayNameOverride": metadata.overrides["displayName"]}
            if metadata.overrides.get("displayName")
            else {}
        ),
        **(
            {"contextWindowDeclared": metadata.overrides["contextWindow"]}
            if metadata.overrides.get("contextWindow") is not None
            else {}
        ),
        **(
            {"inputModalitiesDeclared": metadata.overrides["inputModalities"]}
            if metadata.overrides.get("inputModalities") is not None
            else {}
        ),
    }


def _patch(raw):
    try:
        changes = ModelMetadata.model_validate(raw).model_dump(exclude_unset=True)
    except ValidationError:
        raise HTTPException(422, "Invalid model metadata.") from None
    if any(
        changes.get(name) is not None and changes[name] <= 0
        for name in ("contextWindow", "maxInputTokens", "maxOutputTokens", "autoCompactTokenLimit")
    ):
        raise HTTPException(400, "Model limits must be positive.")
    for key in ("inputModalities", "outputModalities"):
        if changes.get(key) is not None and any(
            kind not in {"text", "image", "audio"} for kind in changes[key]
        ):
            raise HTTPException(400, "Unsupported model modality.")
    if changes.get("displayName") is not None and not changes["displayName"].strip():
        raise HTTPException(400, "Display name must be nonempty.")
    return changes


def _apply(values, changes):
    values = dict(values)
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def _identity(model_id):
    if not isinstance(model_id, str) or not model_id.strip():
        raise HTTPException(400, "A model ID is required.")
    return model_id.strip()


def catalog_router(
    *, store: ConfigurationStore, catalog: CatalogRefresh, on_changed=None, models=None
) -> APIRouter:
    router = APIRouter(prefix="/api")

    async def changed():
        if on_changed is None:
            return {"status": "pending"}
        try:
            return await on_changed()
        except ValueError, RuntimeError, OSError:
            return {"status": "pending"}

    def rows(config):
        return models() if models is not None else effective_models(config, catalog.snapshot)

    def find(state, name):
        try:
            return ChannelService.find(state, name)
        except ValueError:
            raise HTTPException(404, "Channel not found.") from None

    def known(config, channel):
        snapshot = catalog.snapshot(channel)
        return (
            {
                *(row["id"] for row in snapshot.rows if isinstance(row.get("id"), str)),
                *(item.modelId for item in config.models.custom if item.provider == channel.id),
            }
            if snapshot
            else {
                *channel.models.static,
                *(item.modelId for item in config.models.custom if item.provider == channel.id),
            }
        )

    @router.get("/models")
    def list_models():
        config = store.read().configuration
        channels = {channel.id: channel for channel in config.channels}
        return [
            model_row(model, channels[model.identity.channel_id])
            for model in rows(config)
            if model.identity.channel_id in channels
        ]

    @router.get("/selected-models")
    def selection():
        config = store.read().configuration
        available = {value.id: [] for value in config.channels}
        for model in rows(config):
            if model.identity.channel_id in available:
                available[model.identity.channel_id].append(model.identity.native_id)
        return {
            "selected": {
                value.id: value.models.selected
                for value in config.channels
                if value.models.selected
            },
            "available": available,
            "liveModelCounts": {
                value.id: len(snapshot.rows)
                for value in config.channels
                if (snapshot := catalog.snapshot(value)) is not None
            },
        }

    @router.post("/providers/{name}/models/refresh")
    async def refresh(name: str):
        connection = find(store.read(), name)
        if connection.disabled:
            raise HTTPException(400, "Channel is disabled.")
        snapshot = await catalog.refresh(connection, force=True)
        return {
            "ok": snapshot is None or snapshot.status != "failed",
            "provider": name,
            "models": [row for row in list_models() if row["provider"] == name],
            "discovery": discovery_view(snapshot),
            "catalogRefresh": await changed(),
        }

    @router.get("/providers/{name}/models/status")
    def status(name: str):
        connection = find(store.read(), name)
        snapshot = catalog.snapshot(connection)
        state = (
            "unsupported"
            if connection.disabled or not connection.models.live
            else (
                "not_checked"
                if snapshot is None
                else "failed"
                if snapshot.status == "failed"
                else "ready"
            )
        )
        return {
            "state": state,
            "last_success_at": snapshot.last_success_at if snapshot else None,
            "last_attempt_at": snapshot.checked_at if snapshot else None,
            "error": {
                "code": "catalog_refresh_failed",
                "message": "Model discovery failed.",
                "retryable": True,
            }
            if state == "failed"
            else None,
        }

    @router.get("/aliases")
    def aliases():
        config = store.read().configuration
        result = {}
        for channel in config.channels:
            ids = known(config, channel)
            result[channel.id] = {
                key: {
                    "alias": value.value,
                    "source": value.source,
                    **({"stale": True} if key not in ids else {}),
                }
                for key, value in effective_aliases(config.models, channel, ids).items()
            }
        return {
            "providers": {c.id: c.alias for c in config.channels if c.alias},
            "models": result,
            "defaults": {
                "global": config.models.default_aliases,
                "providers": {
                    c.id: c.models.default_aliases
                    for c in config.channels
                    if c.models.default_aliases is not None
                },
            },
        }

    @router.put("/default-aliases")
    async def default_aliases(body: dict):
        if type(body.get("enabled")) is not bool:
            raise HTTPException(400, "enabled must be boolean.")

        def update(state):
            if "provider" in body:
                value = find(state, body["provider"])
                value.models = value.models.model_copy(update={"default_aliases": body["enabled"]})
            else:
                state.configuration.models = state.configuration.models.model_copy(
                    update={"default_aliases": body["enabled"]}
                )

        store.update(update)
        return {"ok": True, "catalogRefresh": await changed()}

    async def labels(name, body, *, display):
        additions, removals = body.get("set", {}), body.get("remove", [])
        if (
            not isinstance(additions, dict)
            or not isinstance(removals, list)
            or not all(isinstance(key, str) for key in removals)
        ):
            raise HTTPException(400, "Invalid model labels.")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or (not display and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value.strip()))
            for value in additions.values()
        ):
            raise HTTPException(400, "Invalid model label.")

        def update(state):
            connection = find(state, name)
            if display:
                metadata = connection.models.metadata
                values = dict(metadata.overrides)
                for key in set(additions) | set(removals):
                    original = values.get(key, ModelMetadata()).model_dump(exclude_unset=True)
                    fields = _apply(
                        original,
                        {"displayName": additions[key].strip() if key in additions else None},
                    )
                    if fields:
                        values[key] = ModelMetadata.model_validate(fields)
                    else:
                        values.pop(key, None)
                connection.models = connection.models.model_copy(
                    update={"metadata": metadata.model_copy(update={"overrides": values})}
                )
                return {
                    key: value.displayName for key, value in values.items() if value.displayName
                }
            values = {
                key: value
                for key, value in connection.models.aliases.items()
                if key not in removals
            }
            values.update({key: value.strip() for key, value in additions.items()})
            if len({value.lower() for value in values.values()}) != len(values):
                raise HTTPException(409, "Model alias collision.")
            connection.models = connection.models.model_copy(update={"aliases": values})
            return values

        _, result = store.update(update)
        return {"ok": True, "aliases": result, "catalogRefresh": await changed()}

    @router.put("/providers/{name}/model-aliases")
    async def model_aliases(name: str, body: dict):
        return await labels(name, body, display=False)

    @router.put("/providers/{name}/model-display-names")
    async def display_names(name: str, body: dict):
        return await labels(name, body, display=True)

    def rename(state, connection, custom, target):
        previous = custom.modelId
        if previous == target:
            return
        cfg = state.configuration
        old = ModelIdentity(connection.id, previous)
        new = ModelIdentity(connection.id, target)
        existing = known(cfg, connection) - {previous}
        if any(
            ModelIdentity(connection.id, item).routed_selector == new.routed_selector
            for item in existing
        ):
            raise HTTPException(409, "Duplicate model.")
        models_ = connection.models
        for values in (
            models_.aliases,
            models_.adapters,
            models_.metadata.overrides,
            models_.metadata.capabilities,
            models_.metadata.costs,
        ):
            if previous in values:
                if target in values:
                    raise HTTPException(409, "Model settings already exist for this ID.")
                values[target] = values.pop(previous)
        if models_.default == previous:
            models_.default = target
        if cfg.default_channel_id == connection.id and cfg.default_model_id == previous:
            cfg.default_model_id = target
        models_.selected = [target if item == previous else item for item in models_.selected]
        models_.static = [target if item == previous else item for item in models_.static]
        cfg.models.disabled = [
            new.routed_selector if item in {old.namespaced_id, old.routed_selector} else item
            for item in cfg.models.disabled
        ]
        custom.modelId = target

    @router.put("/model-settings")
    async def model_settings(body: dict):
        if "changes" in body:
            try:
                typed = ModelOverrideUpdate.model_validate(body)
            except ValidationError:
                raise HTTPException(422, "Invalid model override.") from None
            name, native, changes = (
                typed.model.channel_id,
                _identity(typed.model.model_id),
                _patch(typed.changes.model_dump(exclude_unset=True)),
            )
        else:
            name, native = body.get("provider"), _identity(body.get("modelId"))
            changes = _patch({key: body[key] for key in ModelMetadata.model_fields if key in body})
        if (
            "cost" in body
            and body["cost"] is not None
            and (not isinstance(body["cost"], dict) or _user_rates(body["cost"])[0] is None)
        ):
            raise HTTPException(400, "Invalid model cost.")

        def update(state):
            connection = find(state, name)
            cfg = state.configuration
            before = cfg.model_dump(exclude_unset=True)
            custom_index = next(
                (
                    i
                    for i, item in enumerate(cfg.models.custom)
                    if item.provider == name and item.modelId == native
                ),
                None,
            )
            custom = cfg.models.custom[custom_index] if custom_index is not None else None
            target = _identity(body.get("newModelId", native))
            if target != native:
                if custom is None:
                    raise HTTPException(400, "Only custom models can be renamed.")
                rename(state, connection, custom, target)
            meta = connection.models.metadata
            metadata_updates = {}
            for field in ("capabilities", "overrides"):
                values = dict(getattr(meta, field))
                raw = values.get(target, ModelMetadata()).model_dump(exclude_unset=True)
                stripped = {key: value for key, value in raw.items() if key not in changes}
                if field == "overrides" and custom is None:
                    stripped = _apply(raw, changes)
                if stripped:
                    values[target] = ModelMetadata.model_validate(stripped)
                else:
                    values.pop(target, None)
                if values != getattr(meta, field):
                    metadata_updates[field] = values
            if custom is not None:
                custom = CustomModel.model_validate(
                    _apply(custom.model_dump(exclude_unset=True), changes)
                )
                cfg.models.custom[custom_index] = custom
            if "cost" in body:
                costs = dict(meta.costs)
                if body["cost"] is None:
                    costs.pop(target, None)
                else:
                    costs[target] = body["cost"]
                if costs != meta.costs:
                    metadata_updates["costs"] = costs
            if metadata_updates:
                meta = meta.model_copy(update=metadata_updates)
                connection.models = connection.models.model_copy(update={"metadata": meta})
            has = bool(
                meta.overrides.get(target)
                or meta.capabilities.get(target)
                or target in meta.costs
                or (custom and set(custom.model_fields_set) & ModelMetadata.model_fields.keys())
            )
            return before != cfg.model_dump(exclude_unset=True), has

        _, (saved, has) = store.update(update)
        application = await changed() if saved else {"status": "skipped"}
        if application.get("status") == "pending":
            application = {**application, "status": "failed", "retryable": True}
        return {
            "ok": True,
            "provider": name,
            "modelId": native,
            "changed": saved,
            "saved": saved,
            "hasOverrides": has,
            "catalogRefresh": application,
        }

    @router.get("/custom-models")
    def custom_models():
        return [
            row.model_dump(exclude_unset=True) for row in store.read().configuration.models.custom
        ]

    @router.post("/custom-models", status_code=201)
    async def add_custom(body: dict):
        model_id = _identity(body.get("modelId"))
        patch = _patch({key: body[key] for key in ModelMetadata.model_fields if key in body})

        def update(state):
            channel = find(state, body.get("provider"))
            selector = ModelIdentity(channel.id, model_id).routed_selector
            if any(
                row.provider == channel.id
                and ModelIdentity(channel.id, row.modelId).routed_selector == selector
                for row in state.configuration.models.custom
            ):
                raise HTTPException(409, "Duplicate model.")
            row = CustomModel(
                id=str(uuid4()),
                provider=channel.id,
                modelId=model_id,
                addedAt=datetime.now(UTC).isoformat(),
                **_apply({}, patch),
            )
            cfg = state.configuration
            cfg.models = cfg.models.model_copy(update={"custom": [*cfg.models.custom, row]})
            return row

        _, custom = store.update(update)
        return {**custom.model_dump(exclude_unset=True), "catalogRefresh": await changed()}

    @router.put("/custom-models/{identity}")
    async def edit_custom(identity: str, body: dict):
        patch = _patch({key: body[key] for key in ModelMetadata.model_fields if key in body})

        def update(state):
            custom = state.configuration.models.custom
            found = next(
                (index for index, value in enumerate(custom) if value.id == identity), None
            )
            if found is None:
                raise HTTPException(404, "Model not found.")
            original = custom[found]
            if "modelId" in body:
                rename(state, find(state, original.provider), original, _identity(body["modelId"]))
            result = CustomModel.model_validate(
                _apply(original.model_dump(exclude_unset=True), patch)
            )
            custom[found] = result
            return result

        _, custom = store.update(update)
        return {**custom.model_dump(exclude_unset=True), "catalogRefresh": await changed()}

    @router.delete("/custom-models/{identity}")
    async def delete_custom(identity: str):
        def update(state):
            cfg = state.configuration
            cfg.models = cfg.models.model_copy(
                update={"custom": [row for row in cfg.models.custom if row.id != identity]}
            )

        store.update(update)
        return {"ok": True, "catalogRefresh": await changed()}

    return router
