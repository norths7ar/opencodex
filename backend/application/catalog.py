"""Catalog queries and model edits share one transactional application boundary."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from backend.catalog.identity import ModelIdentity, effective_aliases, legacy_disabled_candidates
from backend.catalog.view import effective_models
from backend.configuration.models import CustomModel, ModelMetadata
from backend.providers.channels import ChannelService
from backend.telemetry.pricing import _user_rates

from .apply import apply_client_changes, catalog_application
from .contracts import ModelOverrideUpdate
from .discovery import discovery_state
from .errors import ApplicationError


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
        raise ApplicationError(422, "Invalid model metadata.") from None
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
        raise ApplicationError(400, "A model ID is required.")
    return model_id.strip()


class CatalogManagement:
    def __init__(self, store, catalog, *, on_changed=None, models=None):
        self.store, self.catalog, self.on_changed, self.models = (
            store,
            catalog,
            on_changed,
            models,
        )

    async def changed(self):
        return await apply_client_changes(self.on_changed)

    def rows(self, config):
        return (
            self.models()
            if self.models is not None
            else effective_models(config, self.catalog.snapshot)
        )

    def find(self, state, name):
        try:
            return ChannelService.find(state, name)
        except ValueError:
            raise ApplicationError(404, "Channel not found.") from None

    def known(self, config, channel):
        snapshot = self.catalog.snapshot(channel)
        return (
            {
                *(row["id"] for row in snapshot.rows if isinstance(row.get("id"), str)),
                *(item.modelId for item in config.models.custom if item.provider == channel.id),
            }
            if snapshot
            else {
                *(channel.models.static if not channel.models.live else ()),
                *(item.modelId for item in config.models.custom if item.provider == channel.id),
            }
        )

    def list_models(self):
        config = self.store.read().configuration
        channels = {channel.id: channel for channel in config.channels}
        return [
            model_row(model, channels[model.identity.channel_id])
            for model in self.rows(config)
            if model.identity.channel_id in channels
        ]

    def selection(self):
        config = self.store.read().configuration
        available = {value.id: [] for value in config.channels}
        for model in self.rows(config):
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
                if (snapshot := self.catalog.snapshot(value)) is not None
            },
        }

    async def refresh(self, name: str):
        connection = self.find(self.store.read(), name)
        if connection.disabled:
            raise ApplicationError(400, "Channel is disabled.")
        outcome = await self.catalog.refresh(connection, force=True)
        snapshot = outcome.snapshot if outcome else None
        return {
            "ok": snapshot is None or snapshot.status != "failed",
            "provider": name,
            "models": [row for row in self.list_models() if row["provider"] == name],
            "discovery": discovery_state(
                connection, snapshot, self.store.read().configuration
            ).model_dump(),
            "catalogRefresh": catalog_application(outcome),
        }

    def status(self, name: str):
        state = self.store.read()
        connection = self.find(state, name)
        snapshot = self.catalog.snapshot(connection)
        return discovery_state(connection, snapshot, state.configuration)

    def aliases(self):
        config = self.store.read().configuration
        result = {}
        for channel in config.channels:
            ids = self.known(config, channel)
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

    async def default_aliases(self, body: dict):
        if type(body.get("enabled")) is not bool:
            raise ApplicationError(400, "enabled must be boolean.")

        def update(state):
            if "provider" in body:
                value = self.find(state, body["provider"])
                value.models = value.models.model_copy(update={"default_aliases": body["enabled"]})
            else:
                state.configuration.models = state.configuration.models.model_copy(
                    update={"default_aliases": body["enabled"]}
                )

        self.store.update(update)
        return {"ok": True, "catalogRefresh": await self.changed()}

    async def labels(self, name, body, *, display):
        additions, removals = (body.get("set", {}), body.get("remove", []))
        if (
            not isinstance(additions, dict)
            or not isinstance(removals, list)
            or (not all(isinstance(key, str) for key in removals))
        ):
            raise ApplicationError(400, "Invalid model labels.")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or (not display and (not re.fullmatch("[A-Za-z0-9][A-Za-z0-9._-]*", value.strip())))
            for value in additions.values()
        ):
            raise ApplicationError(400, "Invalid model label.")

        def update(state):
            connection = self.find(state, name)
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
                raise ApplicationError(409, "Model alias collision.")
            connection.models = connection.models.model_copy(update={"aliases": values})
            return values

        _, result = self.store.update(update)
        return {"ok": True, "aliases": result, "catalogRefresh": await self.changed()}

    def rename(self, state, connection, custom, target):
        previous = custom.modelId
        if previous == target:
            return
        cfg = state.configuration
        old = ModelIdentity(connection.id, previous)
        new = ModelIdentity(connection.id, target)
        existing = self.known(cfg, connection) - {previous}
        affected = {
            spelling: candidates
            for spelling, candidates in legacy_disabled_candidates(
                cfg.models, connection, {*existing, previous}
            ).items()
            if previous in candidates
        }
        if any(len(candidates) > 1 for candidates in affected.values()):
            raise ApplicationError(409, "Resolve the ambiguous disabled-model selector first.")
        if target in existing:
            raise ApplicationError(409, "Duplicate model.")
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
                    raise ApplicationError(409, "Model settings already exist for this ID.")
                values[target] = values.pop(previous)
        if models_.default == previous:
            models_.default = target
        if cfg.default_channel_id == connection.id and cfg.default_model_id == previous:
            cfg.default_model_id = target
        models_.selected = [target if item == previous else item for item in models_.selected]
        models_.static = [target if item == previous else item for item in models_.static]
        cfg.models.disabled = [
            new.namespaced_id if item == old.namespaced_id else item for item in cfg.models.disabled
        ]
        if affected and new.namespaced_id not in cfg.models.disabled:
            cfg.models.disabled.append(new.namespaced_id)
        cfg.models.legacy_disabled = [
            item for item in cfg.models.legacy_disabled if item not in affected
        ]
        custom.modelId = target

    async def model_settings(self, body: dict):
        try:
            typed = ModelOverrideUpdate.model_validate(body)
        except ValidationError:
            raise ApplicationError(422, "Invalid model override.") from None
        name, native, changes = (
            typed.model.channel_id,
            _identity(typed.model.model_id),
            _patch(typed.changes.model_dump(exclude_unset=True)),
        )
        if (
            "cost" in body
            and body["cost"] is not None
            and (not isinstance(body["cost"], dict) or _user_rates(body["cost"])[0] is None)
        ):
            raise ApplicationError(400, "Invalid model cost.")

        def update(state):
            connection = self.find(state, name)
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
            target = _identity(typed.new_model_id or native)
            if target != native:
                if custom is None:
                    raise ApplicationError(400, "Only custom models can be renamed.")
                self.rename(state, connection, custom, target)
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
            return (before != cfg.model_dump(exclude_unset=True), has)

        _, (saved, has) = self.store.update(update)
        application = await self.changed() if saved else {"status": "skipped"}
        return {
            "ok": True,
            "provider": name,
            "modelId": native,
            "changed": saved,
            "saved": saved,
            "hasOverrides": has,
            "catalogRefresh": application,
        }

    def custom_models(self):
        return [
            row.model_dump(exclude_unset=True)
            for row in self.store.read().configuration.models.custom
        ]

    async def add_custom(self, body: dict):
        model_id = _identity(body.get("modelId"))
        patch = _patch({key: body[key] for key in ModelMetadata.model_fields if key in body})

        def update(state):
            channel = self.find(state, body.get("provider"))
            if any(
                row.provider == channel.id and row.modelId == model_id
                for row in state.configuration.models.custom
            ):
                raise ApplicationError(409, "Duplicate model.")
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

        _, custom = self.store.update(update)
        return {**custom.model_dump(exclude_unset=True), "catalogRefresh": await self.changed()}

    async def edit_custom(self, identity: str, body: dict):
        patch = _patch({key: body[key] for key in ModelMetadata.model_fields if key in body})

        def update(state):
            custom = state.configuration.models.custom
            found = next(
                (index for index, value in enumerate(custom) if value.id == identity), None
            )
            if found is None:
                raise ApplicationError(404, "Model not found.")
            original = custom[found]
            if "modelId" in body:
                self.rename(
                    state, self.find(state, original.provider), original, _identity(body["modelId"])
                )
            result = CustomModel.model_validate(
                _apply(original.model_dump(exclude_unset=True), patch)
            )
            custom[found] = result
            return result

        _, custom = self.store.update(update)
        return {**custom.model_dump(exclude_unset=True), "catalogRefresh": await self.changed()}

    async def delete_custom(self, identity: str):

        def update(state):
            cfg = state.configuration
            cfg.models = cfg.models.model_copy(
                update={"custom": [row for row in cfg.models.custom if row.id != identity]}
            )

        self.store.update(update)
        return {"ok": True, "catalogRefresh": await self.changed()}
