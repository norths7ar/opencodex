"""Channel registration, editing and connection checks, independent of HTTP."""

import asyncio
import re
import time
from uuid import uuid4

from pydantic import ValidationError

from backend.auth.access import is_chatgpt_subscription, validate_subscription
from backend.auth.credentials import apply_credential_change as _credential_change
from backend.auth.storage import resolve_credential
from backend.catalog.selection import reconcile_discovery
from backend.configuration.models import Channel, CredentialRecord
from backend.configuration.store import RevisionConflict
from backend.providers import SERVICES, match_destination

from .apply import apply_client_changes, catalog_application
from .contracts import (
    ChannelConfigurationEdit,
    ChannelConfigurationSaved,
    ChannelDefaultSaved,
    ChannelDetail,
    ChannelList,
    CredentialStatus,
    SaveChannel,
)
from .discovery import discovery_state
from .errors import ApplicationError


class ChannelManagement:
    def __init__(
        self,
        channels,
        authentication,
        catalog,
        *,
        quota=None,
        on_changed=None,
        completion_probe=None,
    ):
        self.channels, self.authentication, self.catalog = (channels, authentication, catalog)
        self.quota, self.on_changed, self.completion_probe = (quota, on_changed, completion_probe)
        self.store, self.auth = (channels.store, authentication.chatgpt)
        self.probe_locks = {}
        self.login_lock = asyncio.Lock()

    def list(self):
        state = self.store.read()
        return ChannelList(
            revision=state.revision,
            default_channel_id=state.configuration.default_channel_id,
            default_model_id=state.configuration.default_model_id,
            channels=[self.detail(state, value) for value in state.configuration.channels],
        )

    def read(self, name):
        state = self.store.read()
        try:
            channel = self.channels.find(state, name)
        except ValueError:
            raise ApplicationError(404, "Channel not found.") from None
        return self.detail(state, channel)

    async def set_default(self, channel_id, model_id=None, revision=None):
        try:
            self.channels.set_default(channel_id, model_id=model_id, expected_revision=revision)
        except RevisionConflict as exc:
            raise ApplicationError(409, str(exc)) from exc
        except ValueError as exc:
            raise ApplicationError(400, str(exc)) from exc
        application = await self.changed()
        return ChannelDefaultSaved.model_validate(
            dict(self.list().model_dump(exclude_unset=True), saved=True, application=application)
        )

    def channel(self, name: str) -> Channel:
        try:
            return self.channels.get(name)
        except ValueError:
            raise ApplicationError(404, "Channel not found.") from None

    async def changed(self, *, refresh=False):
        if refresh:
            self.catalog.request_refresh()
        return await apply_client_changes(self.on_changed)

    def credential_status(self, value, record):
        if value.auth_mode == "local":
            return CredentialStatus(state="configured")
        if value.auth_mode == "oauth":
            if not is_chatgpt_subscription(value):
                return CredentialStatus(state="unavailable")
            status = self.auth.status()
            if status.get("credentialUnavailable"):
                return CredentialStatus(state="unavailable")
            if status.get("needsReauth"):
                return CredentialStatus(state="reauth_required")
            if status.get("activeAccountId"):
                return CredentialStatus(state="configured")
            return CredentialStatus(state="unknown" if status.get("pending") else "missing")
        if not (record.env_var or record.protected_key):
            return CredentialStatus(state="missing", label=record.key_label)
        try:
            resolve_credential(record)
        except ValueError, RuntimeError, OSError:
            return CredentialStatus(state="unavailable", label=record.key_label)
        return CredentialStatus(state="configured", label=record.key_label)

    def detail(self, state, value):
        record = state.credentials.get(value.id, CredentialRecord())
        return ChannelDetail(
            channel=value,
            credential=self.credential_status(value, record),
            has_custom_headers=bool(record.headers),
            revision=state.revision,
            discovery=discovery_state(value, self.catalog.snapshot(value), state.configuration),
        )

    def editor_view(self, state):
        return ChannelConfigurationEdit(
            revision=state.revision,
            channels=state.configuration.channels,
            default_channel_id=state.configuration.default_channel_id,
            default_model_id=state.configuration.default_model_id,
        )

    def presets(self):
        oauth_id = next(
            (
                value.id
                for value in self.store.read().configuration.channels
                if is_chatgpt_subscription(value)
            ),
            "openai",
        )
        return {
            "providers": [
                {
                    "id": "openai-apikey" if service.id == "openai" else service.id,
                    "serviceId": service.id,
                    "label": service.name,
                    "auth": service.auth_mode,
                    "adapter": service.destinations[0].default_protocol,
                    "baseUrl": service.destinations[0].base_url,
                    "baseUrlChoices": [
                        {
                            "id": destination.id,
                            "label": destination.id,
                            "baseUrl": destination.base_url,
                            "adapter": destination.default_protocol,
                        }
                        for destination in service.destinations
                    ],
                    **({"oauthProvider": oauth_id} if service.id == "chatgpt" else {}),
                }
                for service in SERVICES
            ]
        }

    def parse_edit(self, raw) -> SaveChannel:
        try:
            return SaveChannel.model_validate(raw)
        except ValidationError:
            raise ApplicationError(422, "Invalid channel or credential settings.") from None

    def ensure_channel_identity(self, candidate, others):
        alias = candidate.alias or ""
        if alias and (not re.fullmatch("[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias)):
            raise ApplicationError(400, "Invalid channel alias.")
        names = {candidate.id.lower(), alias.lower()} - {""}
        if any(names & {other.id.lower(), (other.alias or "").lower()} for other in others):
            raise ApplicationError(409, "Channel ID or alias conflicts with another connection.")

    def normalize_channel(self, candidate):
        if candidate.auth_mode == "oauth":
            try:
                validate_subscription(candidate)
            except ValueError as exc:
                raise ApplicationError(400, str(exc)) from exc
            return candidate
        match = match_destination(candidate)
        return candidate.model_copy(update={"service_id": match.service.id if match else "custom"})

    async def save(self, edit: SaveChannel, *, revision: int | None = None, create=False):
        candidate = self.normalize_channel(edit.channel)
        current = self.store.read()
        revision = current.revision if revision is None else revision
        others = [value for value in current.configuration.channels if value.id != candidate.id]
        previous = next(
            (value for value in current.configuration.channels if value.id == candidate.id), None
        )
        if create and previous is not None:
            raise ApplicationError(409, "Channel already exists.")
        self.ensure_channel_identity(candidate, others)
        if previous and (previous.auth_mode == "oauth") != (candidate.auth_mode == "oauth"):
            raise ApplicationError(
                409, "Use a separate connection to change subscription authentication."
            )
        if is_chatgpt_subscription(candidate) and any(map(is_chatgpt_subscription, others)):
            raise ApplicationError(409, "A ChatGPT subscription channel already exists.")
        if not current.configuration.default_channel_id and candidate.disabled:
            raise ApplicationError(400, "The first channel must be enabled.")
        if previous is not None:
            metadata = candidate.models.metadata.model_copy(
                update={"initial_selection": previous.models.metadata.initial_selection}
            )
            candidate = candidate.model_copy(
                update={"models": candidate.models.model_copy(update={"metadata": metadata})}
            )
        try:
            value = self.channels.save(
                candidate,
                credentials=lambda previous: _credential_change(edit, previous),
                expected_revision=revision,
            )
        except RevisionConflict as exc:
            raise ApplicationError(409, str(exc)) from exc
        except ValueError as exc:
            raise ApplicationError(400, str(exc)) from exc
        except RuntimeError:
            raise ApplicationError(
                503, "The credential could not be protected for this user."
            ) from None
        updated = self.channels.find(value, candidate.id)
        application = await self.changed(refresh=True)
        return (self.detail(value, updated), application, value.revision)

    async def remove_provider(self, name: str, revision: int | None = None):
        removed = self.channel(name)
        try:
            state = self.channels.remove(
                name,
                expected_revision=revision,
                allow_empty=removed.auth_mode == "oauth" and not is_chatgpt_subscription(removed),
            )
        except (ValueError, RevisionConflict) as exc:
            raise ApplicationError(409, str(exc)) from exc
        if is_chatgpt_subscription(removed) and (
            not any(map(is_chatgpt_subscription, state.configuration.channels))
        ):
            await self.auth.logout()
        return {
            "success": True,
            "defaultProvider": state.configuration.default_channel_id,
            "catalogRefresh": await self.changed(refresh=True),
        }

    def key_alias(self, name: str, label: str | None):
        value = self.channel(name)
        record = self.authentication.record(value)
        if value.auth_mode != "key" or not (record.env_var or record.protected_key):
            raise ApplicationError(404, "Credential not found.")
        if label is not None and (
            not isinstance(label, str) or len(label) > 80 or (not label.isprintable())
        ):
            raise ApplicationError(400, "Use a printable label of at most 80 characters.")

        def update(state):
            self.channels.find(state, value.id)
            state.credentials[value.id] = state.credentials[value.id].model_copy(
                update={"key_label": label}
            )

        self.store.update(update)
        return {"success": True}

    async def probe(self, name: str):
        value = self.channel(name)
        if not value.models.live:
            return {"applicable": False, "reason": "static_catalog", "latencyMs": 0}
        start = time.monotonic()
        outcome = await self.catalog.refresh(value, force=True)
        snapshot = outcome.snapshot if outcome else None
        ok = snapshot is not None and snapshot.status == "ok"
        return {
            "ok": ok,
            "latencyMs": round((time.monotonic() - start) * 1000),
            "catalogRefresh": catalog_application(outcome),
            **(
                {"modelCount": len(snapshot.rows)}
                if snapshot is not None and ok
                else {"error": "Model discovery failed."}
            ),
        }

    async def complete(self, name: str, body: dict):
        if body.get("confirm") is not True:
            raise ApplicationError(400, "Confirm the billable completion probe first.")
        value = self.channel(name)
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ApplicationError(400, "Select a model.")
        if value.disabled or self.completion_probe is None:
            return {"applicable": False, "reason": "disabled" if value.disabled else "unavailable"}
        lock = self.probe_locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise ApplicationError(409, "A completion check is already running.")
        async with lock:
            return await self.completion_probe(value, model)

    async def replace_channels(self, body):
        try:
            edit = ChannelConfigurationEdit.model_validate(body)
        except ValidationError:
            raise ApplicationError(422, "Invalid channel configuration.") from None
        if not edit.channels:
            raise ApplicationError(400, "Retain at least one channel.")
        proposed = [self.normalize_channel(value) for value in edit.channels]
        if len({value.id for value in proposed}) != len(proposed):
            raise ApplicationError(409, "Channel IDs must be unique.")
        for value in proposed:
            self.ensure_channel_identity(
                value, [other for other in proposed if other.id != value.id]
            )
        if sum(value.auth_mode == "oauth" for value in proposed) > 1:
            raise ApplicationError(409, "A ChatGPT subscription channel already exists.")
        default = next((value for value in proposed if value.id == edit.default_channel_id), None)
        if default is None or default.disabled:
            raise ApplicationError(400, "Select an enabled default channel.")

        def update(state):
            config = state.configuration
            old = {value.id: value for value in config.channels}
            next_rows = []
            for value in proposed:
                previous = old.get(value.id)
                if previous and (previous.auth_mode == "oauth") != (value.auth_mode == "oauth"):
                    raise ApplicationError(
                        409, "Use a separate connection to change subscription authentication."
                    )
                metadata = value.models.metadata.model_copy(
                    update={
                        "initial_selection": previous.models.metadata.initial_selection
                        if previous
                        else {
                            "version": 1,
                            "registrationId": str(uuid4()),
                            "status": "ready" if value.auth_mode == "oauth" else "pending",
                        }
                    }
                )
                next_rows.append(
                    value.model_copy(
                        update={"models": value.models.model_copy(update={"metadata": metadata})}
                    )
                )
            ids = {value.id for value in next_rows}
            config.channels = next_rows
            config.default_channel_id = default.id
            config.default_model_id = edit.default_model_id
            config.models = config.models.model_copy(
                update={
                    "custom": [model for model in config.models.custom if model.provider in ids],
                    "disabled": [
                        model for model in config.models.disabled if model.split("/", 1)[0] in ids
                    ],
                    "legacy_disabled": [
                        model
                        for model in config.models.legacy_disabled
                        if model.split("/", 1)[0] in ids
                    ],
                }
            )
            state.credentials = {
                channel_id: record
                for channel_id, record in state.credentials.items()
                if channel_id in ids
            }
            for value in next_rows:
                if not value.models.live:
                    config = reconcile_discovery(config, value.id, value.models.static)
            state.configuration = config
            return any(map(is_chatgpt_subscription, old.values())) and (
                not any(map(is_chatgpt_subscription, next_rows))
            )

        async with self.login_lock:
            try:
                updated, retired = self.store.update(update, expected_revision=edit.revision)
            except RevisionConflict as exc:
                raise ApplicationError(409, str(exc)) from exc
            if retired:
                await self.auth.logout()
        application = await self.changed(refresh=True)
        return ChannelConfigurationSaved.model_validate(
            dict(
                self.editor_view(updated).model_dump(exclude_unset=True),
                saved=True,
                application=application,
            )
        )
