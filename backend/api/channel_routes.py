"""Management HTTP routes for channel configuration, credentials, and ChatGPT login."""

import asyncio
import base64
import json
import re
import time
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from backend.auth.access import ChannelAuthentication, validate_subscription
from backend.auth.quota import MAX_REPORT_AGE_MS, now_ms, provider_quota
from backend.auth.storage import protect, resolve_credential
from backend.catalog.refresh import CatalogRefresh
from backend.catalog.selection import reconcile_discovery
from backend.configuration.models import Channel, ChannelId, CredentialRecord, Endpoints, Schema
from backend.configuration.store import RevisionConflict
from backend.providers import SERVICES, match_destination
from backend.providers.channels import ChannelService

from .contracts import ChannelDetail, ChannelView, CredentialStatus, SaveChannel


class ChannelConfigurationEdit(Schema):
    revision: int
    channels: list[Channel]
    default_channel_id: ChannelId | None = None
    default_model_id: str | None = None


def discovery_view(snapshot):
    if snapshot is None:
        return {}
    return {
        "status": snapshot.status,
        "checkedAt": snapshot.checked_at,
        "lastSuccessAt": snapshot.last_success_at,
        "reason": snapshot.reason,
        **({"error": "Model discovery failed."} if snapshot.status == "failed" else {}),
    }


def _credential_change(edit: SaveChannel, previous: CredentialRecord) -> CredentialRecord:
    action = edit.credential
    changes = {}
    if action.action == "set_key":
        value = action.value.get_secret_value().strip().encode()
        changes.update(protected_key=base64.b64encode(protect(value)).decode(), env_var=None)
    elif action.action == "set_environment":
        changes.update(protected_key=None, env_var=action.variable)
    elif action.action == "clear":
        changes.update(protected_key=None, env_var=None)
    if edit.headers is not None:
        headers = {key: value.get_secret_value() for key, value in edit.headers.items()}
        try:
            for name, value in headers.items():
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(
                    ord(char) < 32 or ord(char) == 127 for char in value
                ):
                    raise ValueError
            httpx.Headers(headers)
        except ValueError:
            raise HTTPException(400, "Invalid custom header name or value.") from None
        changes["headers"] = headers
    return previous.model_copy(update=changes)


def channel_router(
    *,
    channels: ChannelService,
    authentication: ChannelAuthentication,
    catalog: CatalogRefresh,
    quota=None,
    on_changed=None,
    completion_probe=None,
    port: int | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    store = channels.store
    auth = authentication.chatgpt
    login_lock = asyncio.Lock()
    probe_locks: dict[str, asyncio.Lock] = {}

    def channel(name: str) -> Channel:
        try:
            return channels.get(name)
        except ValueError:
            raise HTTPException(404, "Channel not found.") from None

    async def changed(*, refresh=False):
        if refresh:
            catalog.request_refresh()
        if on_changed is None:
            return {"status": "pending"}
        try:
            return await on_changed()
        except ValueError, RuntimeError, OSError:
            return {"status": "pending"}

    def credential_status(value, record):
        if value.auth_mode == "local":
            return CredentialStatus(state="configured")
        if value.auth_mode == "oauth":
            status = auth.status()
            if status.get("needsReauth"):
                return CredentialStatus(state="reauth_required")
            if status.get("activeAccountId"):
                return CredentialStatus(state="configured")
            return CredentialStatus(state="unknown" if status.get("pending") else "missing")
        if not (record.env_var or record.protected_key):
            return CredentialStatus(state="missing", label=record.key_label)
        try:
            resolve_credential(record, dotenv_path=authentication.dotenv_path)
        except ValueError, RuntimeError, OSError:
            return CredentialStatus(state="unavailable", label=record.key_label)
        return CredentialStatus(state="configured", label=record.key_label)

    def detail(state, value):
        record = state.credentials.get(value.id, CredentialRecord())
        return ChannelDetail(
            channel=value,
            credential=credential_status(value, record),
            has_custom_headers=bool(record.headers),
        )

    def legacy(value: ChannelDetail):
        cfg = value.channel
        models, endpoint = cfg.models, cfg.endpoints
        snapshot = catalog.snapshot(cfg)
        discovered = discovery_view(snapshot)
        if snapshot and snapshot.status == "ok":
            ids = {row["id"] for row in snapshot.rows}
            discovered["staleDefaultModel"] = models.default if models.default not in ids else None
            discovered["staleAliases"] = {
                key: val for key, val in models.aliases.items() if key not in ids
            }
        return {
            "name": cfg.id,
            "displayName": cfg.name,
            "note": cfg.note,
            "allowPrivateNetwork": cfg.allow_private_network,
            **(
                {"requestPacing": cfg.request_pacing.model_dump(exclude_none=True)}
                if cfg.request_pacing is not None
                else {}
            ),
            **(
                {"preserveResponsesReasoningContent": cfg.preserve_responses_reasoning_content}
                if cfg.preserve_responses_reasoning_content is not None
                else {}
            ),
            "baseUrl": endpoint.base_url,
            "adapter": cfg.protocol,
            "authMode": cfg.auth_mode,
            "serviceId": cfg.service_id,
            "disabled": cfg.disabled,
            "liveModels": models.live,
            "models": models.static,
            "selectedModels": models.selected,
            "modelAdapters": models.adapters,
            "modelAliases": models.aliases,
            "defaultAliases": models.default_aliases,
            "defaultModel": models.default,
            "alias": cfg.alias,
            "responsesPath": endpoint.responses,
            "chatCompletionsPath": endpoint.chat,
            "apiKeyTransport": cfg.api_key_transport,
            "hasApiKey": cfg.auth_mode == "key" and value.credential.state == "configured",
            "hasHeaders": value.has_custom_headers,
            "initialModelSelection": models.metadata.initial_selection,
            "discovery": discovered,
            **cfg.request_policy.model_dump(exclude_none=True),
        }

    @router.get("/channels", response_model=list[ChannelView])
    def list_channels(response: Response):
        state = store.read()
        response.headers["X-Config-Revision"] = str(state.revision)
        rows = [detail(state, value) for value in state.configuration.channels]
        return [
            ChannelView.from_channel(
                row.channel, row.credential, has_headers=row.has_custom_headers
            )
            for row in rows
        ]

    def editor_view(state):
        return ChannelConfigurationEdit(
            revision=state.revision,
            channels=state.configuration.channels,
            default_channel_id=state.configuration.default_channel_id,
            default_model_id=state.configuration.default_model_id,
        )

    @router.get(
        "/channels/configuration",
        response_model=ChannelConfigurationEdit,
        response_model_exclude_unset=True,
    )
    def channels_configuration(response: Response):
        state = store.read()
        response.headers["X-Config-Revision"] = str(state.revision)
        return editor_view(state)

    @router.put(
        "/channels/configuration",
        response_model=ChannelConfigurationEdit,
        response_model_exclude_unset=True,
    )
    async def replace_channels(body: dict, response: Response):
        try:
            edit = ChannelConfigurationEdit.model_validate(body)
        except ValidationError:
            raise HTTPException(422, "Invalid channel configuration.") from None
        if not edit.channels:
            raise HTTPException(400, "Retain at least one channel.")
        proposed = [normalize_channel(value) for value in edit.channels]
        if len({value.id for value in proposed}) != len(proposed):
            raise HTTPException(409, "Channel IDs must be unique.")
        for value in proposed:
            ensure_channel_identity(value, [other for other in proposed if other.id != value.id])
        if sum(value.auth_mode == "oauth" for value in proposed) > 1:
            raise HTTPException(409, "A ChatGPT subscription channel already exists.")
        default = next((value for value in proposed if value.id == edit.default_channel_id), None)
        if default is None or default.disabled:
            raise HTTPException(400, "Select an enabled default channel.")

        def update(state):
            config = state.configuration
            old = {value.id: value for value in config.channels}
            next_rows = []
            for value in proposed:
                previous = old.get(value.id)
                if previous and (previous.auth_mode == "oauth") != (value.auth_mode == "oauth"):
                    raise HTTPException(
                        409, "Use a separate connection to change subscription authentication."
                    )
                metadata = value.models.metadata.model_copy(
                    update={
                        "initial_selection": previous.models.metadata.initial_selection
                        if previous
                        else {
                            "version": 1,
                            "registrationId": str(uuid4()),
                            "status": "pending",
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
            return any(value.auth_mode == "oauth" for value in old.values()) and not any(
                value.auth_mode == "oauth" for value in next_rows
            )

        async with login_lock:
            try:
                updated, retired = store.update(update, expected_revision=edit.revision)
            except RevisionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            if retired:
                await auth.logout()
        await changed(refresh=True)
        response.headers["X-Config-Revision"] = str(updated.revision)
        return editor_view(updated)

    @router.get("/channels/{name}", response_model=ChannelDetail, response_model_exclude_unset=True)
    def read_channel(name: str, response: Response):
        state = store.read()
        response.headers["X-Config-Revision"] = str(state.revision)
        try:
            return detail(state, channels.find(state, name))
        except ValueError:
            raise HTTPException(404, "Channel not found.") from None

    @router.get("/providers")
    def providers():
        state = store.read()
        return [legacy(detail(state, value)) for value in state.configuration.channels]

    @router.get("/config")
    def config():
        state = store.read()
        value = state.configuration
        return {
            "port": port,
            "hostname": "127.0.0.1",
            "revision": state.revision,
            "defaultProvider": value.default_channel_id or "",
            "defaultModelAliases": value.models.default_aliases,
            "maxInboundBodyBytes": value.features.request.max_inbound_body_bytes,
            "oauthOpenBrowser": True,
            "providers": {channel.id: legacy(detail(state, channel)) for channel in value.channels},
        }

    @router.get("/provider-presets")
    def presets():
        oauth_id = next(
            (
                value.id
                for value in store.read().configuration.channels
                if value.auth_mode == "oauth"
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

    def parse_edit(raw) -> SaveChannel:
        try:
            return SaveChannel.model_validate(raw)
        except ValidationError:
            # A validation error must not reflect submitted API keys or custom headers.
            raise HTTPException(422, "Invalid channel or credential settings.") from None

    def ensure_channel_identity(candidate, others):
        alias = candidate.alias or ""
        if alias and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias):
            raise HTTPException(400, "Invalid channel alias.")
        names = {candidate.id.lower(), alias.lower()} - {""}
        if any(names & {other.id.lower(), (other.alias or "").lower()} for other in others):
            raise HTTPException(409, "Channel ID or alias conflicts with another connection.")

    def normalize_channel(candidate):
        if candidate.auth_mode == "oauth":
            try:
                validate_subscription(candidate)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return candidate
        match = match_destination(candidate)
        return candidate.model_copy(update={"service_id": match.service.id if match else "custom"})

    async def save(edit: SaveChannel, *, revision: int | None = None, create=False):
        candidate = normalize_channel(edit.channel)
        current = store.read()
        revision = current.revision if revision is None else revision
        others = [value for value in current.configuration.channels if value.id != candidate.id]
        previous = next(
            (value for value in current.configuration.channels if value.id == candidate.id), None
        )
        if create and previous is not None:
            raise HTTPException(409, "Channel already exists.")
        ensure_channel_identity(candidate, others)
        if previous and (previous.auth_mode == "oauth") != (candidate.auth_mode == "oauth"):
            raise HTTPException(
                409, "Use a separate connection to change subscription authentication."
            )
        if candidate.auth_mode == "oauth" and any(value.auth_mode == "oauth" for value in others):
            raise HTTPException(409, "A ChatGPT subscription channel already exists.")
        if not current.configuration.default_channel_id and candidate.disabled:
            raise HTTPException(400, "The first channel must be enabled.")
        if previous is not None:
            metadata = candidate.models.metadata.model_copy(
                update={"initial_selection": previous.models.metadata.initial_selection}
            )
            candidate = candidate.model_copy(
                update={"models": candidate.models.model_copy(update={"metadata": metadata})}
            )
        try:
            value = channels.save(
                candidate,
                credentials=lambda previous: _credential_change(edit, previous),
                expected_revision=revision,
            )
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError:
            raise HTTPException(
                503, "The credential could not be protected for this user."
            ) from None
        updated = channels.find(value, candidate.id)
        application = await changed(refresh=True)
        return detail(value, updated), application, value.revision

    @router.post(
        "/channels",
        response_model=ChannelDetail,
        response_model_exclude_unset=True,
        status_code=201,
    )
    async def create_channel(body: dict, response: Response, revision: int | None = None):
        result, _, saved_revision = await save(parse_edit(body), revision=revision, create=True)
        response.headers["X-Config-Revision"] = str(saved_revision)
        return result

    @router.put("/channels/{name}", response_model=ChannelDetail, response_model_exclude_unset=True)
    async def save_channel(name: str, body: dict, response: Response, revision: int | None = None):
        edit = parse_edit(body)
        if edit.channel.id != name:
            raise HTTPException(400, "Channel ID does not match the URL.")
        result, _, saved_revision = await save(edit, revision=revision)
        response.headers["X-Config-Revision"] = str(saved_revision)
        return result

    def legacy_edit(name, raw, previous=None):
        if not isinstance(raw, dict):
            raise HTTPException(400, "Expected provider settings.")
        fields = (
            previous.model_dump(exclude_unset=True)
            if previous
            else {"id": name, "name": name, "service_id": "custom", "auth_mode": "key"}
        )
        fields["name"] = raw.get("displayName", fields["name"])
        if "note" in raw:
            fields["note"] = raw["note"]
        for current, legacy_name in (
            ("protocol", "adapter"),
            ("auth_mode", "authMode"),
            ("api_key_transport", "apiKeyTransport"),
            ("disabled", "disabled"),
            ("alias", "alias"),
        ):
            if legacy_name in raw:
                fields[current] = raw[legacy_name]
        endpoint = fields.setdefault("endpoints", {})
        for current, legacy_name in (
            ("base_url", "baseUrl"),
            ("responses", "responsesPath"),
            ("chat", "chatCompletionsPath"),
        ):
            if legacy_name in raw:
                endpoint[current] = raw[legacy_name] or None
        model = fields.setdefault("models", {})
        for current, legacy_name in (
            ("static", "models"),
            ("live", "liveModels"),
            ("selected", "selectedModels"),
            ("default", "defaultModel"),
            ("aliases", "modelAliases"),
            ("adapters", "modelAdapters"),
        ):
            if legacy_name in raw:
                model[current] = raw[legacy_name]
        policy = fields.setdefault("request_policy", {})
        for key in Channel.model_fields["request_policy"].annotation.model_fields:
            if key in raw:
                policy[key] = raw[key]
        return parse_edit(
            {
                "channel": fields,
                **(
                    {"credential": {"action": "set_key", "value": raw["apiKey"]}}
                    if raw.get("apiKey")
                    else {}
                ),
            }
        )

    @router.post("/providers")
    async def add_provider(body: dict):
        if "channel" in body:
            edit = parse_edit(body)
        else:
            name = body.get("name")
            if not isinstance(name, str) or not name:
                raise HTTPException(400, "A channel ID is required.")
            edit = legacy_edit(name, body.get("provider"))
        current = store.read()
        if any(value.id == edit.channel.id for value in current.configuration.channels):
            raise HTTPException(409, "Channel already exists.")
        saved, application, _ = await save(edit, revision=current.revision, create=True)
        if body.get("setDefault") is True:
            channels.set_default(saved.channel.id)
        return {"success": True, "name": saved.channel.id, "catalogRefresh": application}

    @router.patch("/providers")
    async def update_provider(name: str, body: dict):
        if "apiKey" in body or "protectedKey" in body or "headers" in body:
            raise HTTPException(400, "Use the channel credential action.")
        current = store.read()
        previous = channel(name)
        if body == {"setDefault": True}:
            try:
                channels.set_default(name, expected_revision=current.revision)
            except RevisionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            application = await changed()
        else:
            _, application, _ = await save(
                legacy_edit(name, body, previous), revision=current.revision
            )
        return {"success": True, "name": name, "catalogRefresh": application}

    @router.delete("/channels/{name}")
    @router.delete("/providers")
    async def remove_provider(name: str, revision: int | None = None):
        removed = channel(name)
        try:
            state = channels.remove(name, expected_revision=revision)
        except (ValueError, RevisionConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        if removed.auth_mode == "oauth" and not any(
            value.auth_mode == "oauth" for value in state.configuration.channels
        ):
            await auth.logout()
        return {
            "success": True,
            "defaultProvider": state.configuration.default_channel_id,
            "catalogRefresh": await changed(refresh=True),
        }

    @router.get("/providers/keys")
    def list_keys(name: str):
        value = channel(name)
        record = authentication.record(value)
        present = value.auth_mode == "key" and bool(record.env_var or record.protected_key)
        return {
            "activeId": "primary" if present else None,
            "keys": [
                {"id": "primary", "masked": "••••••••", "active": True, "label": record.key_label}
            ]
            if present
            else [],
        }

    @router.post("/providers/keys")
    async def set_key(body: dict):
        state = store.read()
        try:
            value = channels.find(state, body.get("name", ""))
        except ValueError:
            raise HTTPException(404, "Channel not found.") from None
        edit = parse_edit(
            {
                "channel": value.model_dump(),
                "credential": {"action": "set_key", "value": body.get("key")},
            }
        )
        previous = state.credentials.get(value.id, CredentialRecord())
        if previous.env_var or previous.protected_key:
            raise HTTPException(409, "This channel already has an API credential.")
        _, application, _ = await save(edit, revision=state.revision)
        return {"success": True, "id": "primary", "catalogRefresh": application}

    @router.delete("/providers/keys")
    async def clear_key(name: str, id: str):
        if id != "primary":
            raise HTTPException(404, "Credential not found.")
        value = channel(name)
        _, application, _ = await save(
            parse_edit({"channel": value.model_dump(), "credential": {"action": "clear"}})
        )
        return {"success": True, "catalogRefresh": application}

    @router.put("/providers/keys/alias")
    def key_alias(body: dict):
        value = channel(body.get("name", ""))
        label = body.get("alias") or None
        if body.get("id") != "primary" or not list_keys(value.id)["activeId"]:
            raise HTTPException(404, "Credential not found.")
        if label is not None and (
            not isinstance(label, str) or len(label) > 80 or not label.isprintable()
        ):
            raise HTTPException(400, "Use a printable label of at most 80 characters.")

        def update(state):
            channels.find(state, value.id)
            state.credentials[value.id] = state.credentials[value.id].model_copy(
                update={"key_label": label}
            )

        store.update(update)
        return {"success": True}

    @router.put("/providers/{name}/alias")
    async def provider_alias(name: str, body: dict):
        alias = body.get("alias") or None
        if alias is not None and (
            not isinstance(alias, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias)
        ):
            raise HTTPException(400, "Invalid channel alias.")

        def update(state):
            value = channels.find(state, name)
            if alias and any(
                other.id != name
                and alias.lower() in {other.id.lower(), (other.alias or "").lower()}
                for other in state.configuration.channels
            ):
                raise HTTPException(409, "Channel alias conflicts with another connection.")
            value.alias = alias

        channel(name)
        store.update(update)
        return {"ok": True, "alias": alias, "catalogRefresh": await changed()}

    @router.post("/providers/test")
    async def probe(name: str):
        value = channel(name)
        if not value.models.live:
            return {"applicable": False, "reason": "static_catalog", "latencyMs": 0}
        start = time.monotonic()
        snapshot = await catalog.refresh(value, force=True)
        ok = snapshot is not None and snapshot.status == "ok"
        return {
            "ok": ok,
            "latencyMs": round((time.monotonic() - start) * 1000),
            **({"modelCount": len(snapshot.rows)} if ok else {"error": "Model discovery failed."}),
        }

    @router.post("/providers/test-completion")
    async def complete(name: str, body: dict):
        if body.get("confirm") is not True:
            raise HTTPException(400, "Confirm the billable completion probe first.")
        value = channel(name)
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(400, "Select a model.")
        if value.disabled or completion_probe is None:
            return {"applicable": False, "reason": "disabled" if value.disabled else "unavailable"}
        lock = probe_locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "A completion check is already running.")
        async with lock:
            return await completion_probe(value, model)

    def supported(provider):
        if not isinstance(provider, str):
            raise HTTPException(400, "Select a subscription service.")
        if provider not in {"chatgpt", "openai"}:
            config = store.read().configuration
            if not any(
                value.id == provider and value.auth_mode == "oauth" for value in config.channels
            ):
                raise HTTPException(400, "This subscription service is not supported.")

    @router.get("/oauth/providers")
    def oauth_providers():
        return {
            "providers": list(
                dict.fromkeys(
                    [
                        "chatgpt",
                        "openai",
                        *(
                            value.id
                            for value in store.read().configuration.channels
                            if value.auth_mode == "oauth"
                        ),
                    ]
                )
            )
        }

    @router.get("/oauth/status")
    def oauth_status(provider: str):
        supported(provider)
        value = auth.status()
        if quota:
            for account in value.get("accounts", []):
                account.update(quota.account_fields(account["id"]))
        return value

    @router.get("/oauth/accounts")
    async def accounts(request: Request, provider: str, refresh: bool = False):
        supported(provider)
        enriched = request.query_params.get("quota") in {"1", "true"}
        if quota and enriched:
            await quota.read(force=refresh)
        value = auth.status()
        if quota:
            for account in value.get("accounts", []):
                account.update(quota.account_fields(account["id"], enriched=enriched))
        return {
            "accounts": value.get("accounts", []),
            "activeAccountId": value.get("activeAccountId"),
            "revision": auth.revision,
        }

    @router.post("/oauth/login")
    async def login(body: dict):
        provider = body.get("provider")
        supported(provider)
        mode = body.get("flow", "browser")
        account_id = body.get("accountId")
        if mode not in {"browser", "device"}:
            raise HTTPException(400, "Choose browser or device login.")
        if (account_id is not None or body.get("reauth") is True) and not (
            isinstance(account_id, str) and account_id.strip()
        ):
            raise HTTPException(400, "Select the account to reauthorize.")
        async with login_lock:
            state = store.read()
            if not any(value.auth_mode == "oauth" for value in state.configuration.channels):
                name = "openai" if provider in {"openai", "chatgpt"} else provider
                if any(value.id == name for value in state.configuration.channels):
                    raise HTTPException(
                        409, "This channel ID already belongs to an API connection."
                    )
                created = Channel.model_validate(
                    {
                        "id": name,
                        "name": "ChatGPT",
                        "service_id": "chatgpt",
                        "protocol": "openai-responses",
                        "auth_mode": "oauth",
                        "oauth_binding": {"service_id": "chatgpt", "account_ref": "chatgpt"},
                        "endpoints": Endpoints(
                            base_url=next(s for s in SERVICES if s.id == "chatgpt")
                            .destinations[0]
                            .base_url
                        ),
                    }
                )
                await save(SaveChannel(channel=created), revision=state.revision)
            try:
                return await auth.start(
                    device=mode == "device",
                    open_browser=body.get("openBrowser", True) is True,
                    reauth_account_id=account_id.strip() if account_id else None,
                )
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(400, str(exc)) from exc

    @router.post("/oauth/login/cancel")
    async def cancel(body: dict):
        supported(body.get("provider"))
        await auth.cancel()
        return {"ok": True}

    @router.post("/oauth/login/code")
    async def login_code(body: dict):
        supported(body.get("provider"))
        callback = body.get("code", body.get("input"))
        if not isinstance(callback, str):
            raise HTTPException(400, "Paste the complete callback URL.")
        try:
            auth.submit(callback.strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "success": True}

    @router.post("/oauth/logout")
    async def logout(provider: str):
        supported(provider)
        await auth.logout()
        catalog.request_refresh()
        return {"ok": True}

    @router.delete("/oauth/accounts")
    async def delete_account(provider: str, id: str):
        supported(provider)
        if auth.status().get("activeAccountId") != id:
            raise HTTPException(404, "Account not found.")
        return await logout(provider)

    @router.put("/oauth/accounts/alias")
    async def account_alias(body: dict):
        supported(body.get("provider"))
        label = body.get("alias", "")
        if not isinstance(label, str) or len(label) > 80 or (label and not label.isprintable()):
            raise HTTPException(400, "Use a printable label of at most 80 characters.")
        async with auth.lock:
            saved = auth.load()
            if not saved or saved["accountId"] != body.get("accountId", body.get("id")):
                raise HTTPException(404, "Account not found.")
            auth.save({**saved, "alias": label.strip() or None})
        return {"ok": True, "alias": label.strip() or None}

    @router.get("/accounts/events")
    async def account_events(request: Request):
        async def stream():
            revision = auth.revision
            yield f"event: ready\ndata: {json.dumps({'revision': revision})}\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(1)
                if auth.revision != revision:
                    revision = auth.revision
                    value = next(
                        (
                            value
                            for value in store.read().configuration.channels
                            if value.auth_mode == "oauth"
                        ),
                        None,
                    )
                    event = {
                        "provider": value.id if value else "chatgpt",
                        "kind": "oauth",
                        "revision": revision,
                    }
                    yield f"event: account-selection\ndata: {json.dumps(event)}\n\n"
                else:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @router.get("/provider-quotas")
    async def provider_quotas(refresh: bool = False):
        value = next(
            (
                value
                for value in store.read().configuration.channels
                if value.auth_mode == "oauth" and not value.disabled
            ),
            None,
        )
        now = now_ms()
        result = {"generatedAt": now, "reports": []}
        if value is None or quota is None:
            return result
        result["quotaProvider"] = value.id
        snapshot = await quota.read(force=refresh)
        if snapshot is None:
            return result
        projected = provider_quota(snapshot.quota)
        if projected and now - projected["updatedAt"] < MAX_REPORT_AGE_MS:
            result["reports"].append(
                {
                    "provider": value.id,
                    "label": "ChatGPT",
                    "source": "chatgpt:wham",
                    "updatedAt": projected["updatedAt"],
                    "quota": projected,
                }
            )
        if snapshot.checked_at:
            result["checkedAt"] = snapshot.checked_at
        if snapshot.failure or not result["reports"]:
            result["quotaUnavailable"] = True
            if snapshot.failure:
                result["quotaFailure"] = snapshot.failure
        return result

    return router
