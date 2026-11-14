"""Original GUI management contracts for independent API connections."""

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .config import ConfigStore, Provider
from .gateway import Gateway
from .model_management import model_policy_router
from .model_policy import initialize_selection, mark_preset_diverged
from .models import (
    MODEL_FIELDS,
    REGISTRY,
    ModelInventory,
    preset_seed,
    routed_slug,
    separate_preset_metadata,
)
from .oauth_api import oauth_router
from .quota_api import quota_router
from .subscription_channels import auth_channels, retired_auth_services


def management_router(
    store: ConfigStore,
    gateway: Gateway,
    directory: Path,
    port: int,
    *,
    inventory: ModelInventory,
    quota,
    kimi_quota,
    catalog_refresh,
    stopping: asyncio.Event,
):
    router = APIRouter(prefix="/api")
    subscriptions = auth_channels(gateway)
    router.include_router(quota_router(store, quota, kimi_quota))
    probe_locks: dict[str, asyncio.Lock] = {}

    def provider_dto(provider):
        data = provider.model_dump(
            exclude={"id", "name", "responses", "headers"}, exclude_none=True
        )
        try:
            data["hasApiKey"] = bool(store.credential(provider))
        except ValueError, RuntimeError:
            data["hasApiKey"] = False
        data["hasHeaders"] = bool(provider.headers)
        return data

    def editor_dto(config):
        return {
            "defaultProvider": config.defaultProvider or "",
            "providers": {
                p.id: {
                    k: v
                    for k, v in provider_dto(p).items()
                    if k
                    not in {
                        "hasApiKey",
                        "hasHeaders",
                        "xaiResponsesOptInState",
                        "initialModelSelection",
                    }
                }
                for p in config.providers
            },
        }

    def find(config, name):
        provider = next((p for p in config.providers if p.id == name), None)
        if provider is None:
            raise HTTPException(404, "Provider not found.")
        return provider

    async def converge():
        return {"catalogRefresh": await catalog_refresh()}

    @router.get("/capabilities")
    def capabilities():
        return {
            "runtime": "python",
            "pages": [
                "home",
                "providers",
                "models",
                "integrations",
                "logs",
                "usage",
                "startup",
                "subagents",
                "dashboard",
                "storage",
            ],
            "usage": True,
            "debugLogs": True,
            "quota": True,
            "clients": ["codex", "dsh"],
            "adapters": ["openai-chat", "openai-responses", "anthropic"],
            "oauth": True,
            "oauthProviders": list(subscriptions),
        }

    @router.get("/config")
    def config():
        value = store.load()
        return {
            "port": port,
            "hostname": "127.0.0.1",
            "defaultProvider": value.defaultProvider or "",
            "defaultModelAliases": value.defaultModelAliases,
            "oauthOpenBrowser": True,
            "providers": {p.id: provider_dto(p) for p in value.providers},
        }

    @router.get("/access")
    def access():
        return {"baseUrl": f"http://127.0.0.1:{port}/v1", "requiresApiKey": False}

    @router.get("/providers")
    def providers():
        return [
            {
                "name": p.id,
                **provider_dto(p),
                **({"discovery": inventory.discovery[p.id]} if p.id in inventory.discovery else {}),
            }
            for p in store.load().providers
        ]

    @router.get("/provider-presets")
    def presets():
        return {"providers": REGISTRY["presets"]}

    @router.get("/protocols")
    def protocol_summary(provider: str):
        value = store.provider(provider)
        overrides = [
            {"model": model, "adapter": adapter, "source": "operator"}
            for model, adapter in getattr(value, "modelAdapters", {}).items()
        ]
        return {
            "provider": {
                "name": value.id,
                "adapter": value.adapter,
                "adapterSource": "provider-default",
                "authMode": value.authMode,
                "upstream": {
                    "openai-chat": "chat",
                    "openai-responses": "responses",
                    "anthropic": "messages",
                }[value.adapter],
                "modelOverrides": overrides[:64],
                **({"modelOverridesTruncated": True} if len(overrides) > 64 else {}),
            }
        }

    @router.post("/providers")
    async def add_provider(body: dict):
        name = str(body.get("name", "")).strip()
        submitted = body.get("provider")
        if not isinstance(submitted, dict):
            raise HTTPException(400, "provider must be an object")
        if submitted.get("authMode") == "forward" and name == "openai":
            submitted = {**submitted, "authMode": "oauth", "statelessResponses": True}
        with store.mutation_lock:
            config = store.load()
            previous = next((p for p in config.providers if p.id == name), None)
            raw = previous.model_dump() if previous else {}
            raw.update(preset_seed(submitted))
            if submitted.get("apiKey"):
                raw["apiKeyEnv"] = None
            raw.update(id=name, name=name)
            raw.pop("protectedKey", None)
            candidate = Provider.model_validate(raw)
            if previous:
                config.providers[config.providers.index(previous)] = candidate
            else:
                initialize_selection(candidate)
                config.providers.append(candidate)
            if not config.defaultProvider or body.get("setDefault"):
                config.defaultProvider = name
                config.defaultModel = candidate.defaultModel
            store.save(config)
        inventory.invalidate(name)
        return {"success": True, "name": name, **await converge()}

    @router.patch("/providers")
    async def update_provider(name: str, body: dict):
        with store.mutation_lock:
            config = store.load()
            provider = find(config, name)
            separate_preset_metadata(provider)
            if "apiKey" in body:
                raise ValueError("Use the provider credential action to replace its key.")
            if body.get("disabled") and config.defaultProvider == name:
                raise HTTPException(
                    409,
                    {
                        "code": "default_provider_disabled",
                        "error": "Select another default provider first.",
                    },
                )
            if body.get("setDefault"):
                if provider.disabled:
                    raise ValueError("The default provider cannot be disabled.")
                config.defaultProvider = name
            raw = provider.model_dump()
            raw.update(
                {
                    k: v
                    for k, v in body.items()
                    if k not in {"setDefault", "id", "name", "protectedKey"}
                }
            )
            updated = Provider.model_validate(raw)
            config.providers[config.providers.index(provider)] = updated
            if config.defaultProvider == name:
                config.defaultModel = updated.defaultModel
            store.save(config)
        inventory.invalidate(name)
        return {"success": True, "name": name, **await converge()}

    @router.delete("/providers")
    async def delete_provider(name: str):
        with store.mutation_lock:
            config = store.load()
            removed_provider = find(config, name)
            if len(config.providers) == 1:
                raise HTTPException(
                    409, {"code": "last_provider", "error": "Cannot remove the last provider."}
                )
            remaining = [p for p in config.providers if p.id != name]
            if config.defaultProvider == name:
                default = next((p for p in remaining if not p.disabled), None)
                if not default:
                    raise ValueError("Enable another provider before removing the default.")
                config.defaultProvider, config.defaultModel = default.id, default.defaultModel
            config.providers = remaining
            config.customModels = [m for m in config.customModels if m["provider"] != name]
            config.disabledModels = [
                m for m in config.disabledModels if not m.startswith(name + "/")
            ]
            store.save(config)
        if removed_provider.authMode == "oauth":
            for auth in retired_auth_services(subscriptions, {name}, config.providers):
                await auth.logout()
        inventory.invalidate(name)
        return {"success": True, "defaultProvider": config.defaultProvider, **await converge()}

    @router.put("/providers")
    async def replace_providers(body: dict):
        with store.mutation_lock:
            config = store.load()
            had_subscriptions = {p.id for p in config.providers if p.authMode == "oauth"}
            if body.get("baseline") != editor_dto(config):
                raise HTTPException(
                    409,
                    {
                        "code": "stale_provider_editor_baseline",
                        "error": "Provider configuration changed; reload before saving.",
                    },
                )
            next_value = body.get("next", {})
            rows = []
            for name, raw in next_value.get("providers", {}).items():
                if any(k in raw for k in ("apiKey", "protectedKey", "headers")):
                    raise ValueError(
                        "Credentials are edited through provider actions, not the JSON editor."
                    )
                previous = next((p for p in config.providers if p.id == name), None)
                fields = {**raw, "id": name, "name": name}
                if previous:
                    fields["headers"] = previous.headers
                    if getattr(previous, "initialModelSelection", None) is not None:
                        fields["initialModelSelection"] = previous.initialModelSelection
                row = Provider.model_validate(fields)
                if previous is None:
                    initialize_selection(row)
                rows.append(row)
            removed = {p.id for p in config.providers} - {p.id for p in rows}
            config.customModels = [m for m in config.customModels if m["provider"] not in removed]
            config.disabledModels = [
                m for m in config.disabledModels if m.split("/", 1)[0] not in removed
            ]
            config.providers = rows
            config.defaultProvider = next_value.get("defaultProvider")
            default = find(config, config.defaultProvider)
            if default.disabled:
                raise ValueError("The default provider cannot be disabled.")
            config.defaultModel = default.defaultModel
            store.save(config)
        removed_subscriptions = had_subscriptions - {p.id for p in rows if p.authMode == "oauth"}
        for auth in retired_auth_services(subscriptions, removed_subscriptions, rows):
            await auth.logout()
        inventory.invalidate()
        return {"success": True, **await converge()}

    @router.get("/providers/keys")
    def keys(name: str):
        provider = store.provider(name)
        present = provider_dto(provider)["hasApiKey"]
        return {
            "activeId": "primary" if present else None,
            "keys": [
                {
                    "id": "primary",
                    "masked": "••••••••",
                    "active": True,
                    "label": getattr(provider, "keyLabel", ""),
                }
            ]
            if present
            else [],
        }

    @router.put("/providers/keys/alias")
    def key_alias(body: dict):
        if body.get("id") != "primary":
            raise HTTPException(404, "Credential not found.")
        with store.mutation_lock:
            config = store.load()
            provider = find(config, body.get("name"))
            alias = body.get("alias")
            if alias is not None and not isinstance(alias, str):
                raise HTTPException(400, "alias must be a string or null")
            provider.keyLabel = (alias or "").strip()
            store.save(config)
        return {"success": True}

    @router.post("/providers/keys")
    async def set_key(body: dict):
        with store.mutation_lock:
            config = store.load()
            provider = find(config, body.get("name"))
            if provider_dto(provider)["hasApiKey"]:
                raise ValueError(
                    "Each connection has one API key. Remove it to replace it, or add an independent provider."
                )
            key = body.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ValueError("API key is required.")
            provider.apiKey = key.strip()
            provider.apiKeyEnv = None
            store.save(config)
        inventory.invalidate(provider.id)
        return {"success": True, "id": "primary", **await converge()}

    @router.delete("/providers/keys")
    async def remove_key(name: str, id: str):
        if id != "primary":
            raise HTTPException(404, "Credential not found.")
        with store.mutation_lock:
            config = store.load()
            provider = find(config, name)
            provider.apiKeyEnv = None
            store.save(config, clear_keys={name})
        inventory.invalidate(name)
        return {"success": True, **await converge()}

    @router.post("/providers/test")
    async def probe(name: str):
        provider = store.provider(name)
        if not provider.liveModels:
            return {"applicable": False, "reason": "static_catalog", "latencyMs": 0}
        start = time.monotonic()
        try:
            if provider.disabled:
                raise ValueError("Provider is disabled.")
            rows = await inventory.discover(provider, force=True)
            if inventory.discovery.get(name, {}).get("status") == "failed":
                raise ValueError(
                    inventory.discovery[name].get("error") or "Model discovery failed."
                )
            return {
                "ok": True,
                "latencyMs": round((time.monotonic() - start) * 1000),
                "modelCount": len(rows),
            }
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            return {
                "ok": False,
                "latencyMs": round((time.monotonic() - start) * 1000),
                "error": str(exc),
            }

    @router.post("/providers/test-completion")
    async def completion(name: str, body: dict):
        if body.get("confirm") is not True:
            raise ValueError("Confirm the billable completion probe first.")
        provider = store.provider(name)
        if provider.disabled:
            return {"applicable": False, "reason": "disabled"}
        lock = probe_locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "A completion check is already running.")
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("Select a model.")
        start = time.monotonic()
        async with lock:
            error = None
            try:
                result = await gateway.check(provider, model, max_tokens=32)
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                result = {}
                error = {"code": "upstream", "message": str(exc)}
        return {
            "applicable": True,
            "ok": error is None,
            "provider": name,
            "requestedModel": model,
            "wireModel": model,
            "wire": provider.adapter,
            "upstreamModel": result.get("upstreamModel"),
            "modelComparison": "match"
            if result.get("upstreamModel") == model
            else "mismatch"
            if result.get("upstreamModel")
            else "unknown",
            "identityVerified": False,
            "ttftMs": None,
            "totalLatencyMs": round((time.monotonic() - start) * 1000),
            "outputTokenCap": 32,
            "termination": "failed" if error else "completed",
            "error": error,
        }

    @router.get("/models")
    async def models():
        return await inventory.rows()

    @router.post("/providers/{name}/models/refresh")
    async def refresh_provider_models(name: str):
        provider = store.provider(name)
        if provider.disabled:
            raise ValueError("Provider is disabled.")
        await inventory.discover(provider, force=True)
        rows = [row for row in await inventory.rows() if row["provider"] == name]
        discovery = inventory.discovery.get(name, {})
        return {
            "ok": discovery.get("status") != "failed",
            "provider": name,
            "models": rows,
            "discovery": discovery,
            **await converge(),
        }

    @router.get("/selected-models")
    async def selected():
        return await inventory.selection()

    @router.put("/selected-models")
    async def select_models(body: dict):
        values = body.get("models")
        if not isinstance(values, list) or any(not isinstance(m, str) for m in values):
            raise ValueError("models must be an array of model IDs.")
        with store.mutation_lock:
            config = store.load()
            provider = find(config, body.get("provider"))
            provider.selectedModels = list(dict.fromkeys(values))
            mark_preset_diverged(provider)
            store.save(config)
        return {"ok": True, "provider": provider.id, "selected": values, **await converge()}

    @router.put("/model-visibility")
    async def visibility(body: dict):
        if body.get("scope") not in {"models", "provider"} or not isinstance(
            body.get("enabled"), bool
        ):
            raise ValueError("Invalid model visibility operation.")
        targets = body.get("targets", [])
        if not targets or any(
            not isinstance(t, dict) or not isinstance(t.get("id"), str) for t in targets
        ):
            raise ValueError("Model visibility targets are required.")
        with store.mutation_lock:
            config = store.load()
            provider = find(config, body.get("provider"))
            disabled = set(config.disabledModels)
            ids = [t["id"] for t in targets]
            if body["enabled"]:
                if body["scope"] == "provider":
                    provider.selectedModels = []
                    disabled = {s for s in disabled if not s.startswith(provider.id + "/")}
                else:
                    if provider.selectedModels:
                        provider.selectedModels = list(
                            dict.fromkeys([*provider.selectedModels, *ids])
                        )
                    for model in ids:
                        disabled.discard(routed_slug(provider.id, model))
                        disabled.discard(f"{provider.id}/{model}")
            else:
                disabled.update(routed_slug(provider.id, model) for model in ids)
            config.disabledModels = sorted(disabled)
            mark_preset_diverged(provider)
            store.save(config)
        return {
            "ok": True,
            "disabled": config.disabledModels,
            "selected": provider.selectedModels,
            "clientIntegrations": [],
            **await converge(),
        }

    @router.put("/disabled-models")
    async def disabled_models(body: dict):
        with store.mutation_lock:
            config = store.load()
            config.disabledModels = body.get("models", [])
            store.save(config)
        return {"ok": True, "disabled": config.disabledModels, **await converge()}

    @router.put("/model-settings")
    async def model_settings(body: dict):
        from .usage_pricing import _user_rates

        maps = {
            "displayName": "modelDisplayNames",
            "contextWindow": "modelContextWindows",
            "inputModalities": "modelInputModalities",
            "reasoningEfforts": "modelReasoningEfforts",
            "defaultReasoningEffort": "modelDefaultReasoningEfforts",
        }
        with store.mutation_lock:
            config = store.load()
            before = config.model_dump()
            provider = find(config, body.get("provider"))
            separate_preset_metadata(provider)
            model = body.get("modelId")
            if not isinstance(model, str) or not model:
                raise ValueError("modelId is required.")
            custom_fields(body)
            if (
                "displayName" in body
                and body["displayName"] is not None
                and (not isinstance(body["displayName"], str) or not body["displayName"].strip())
            ):
                raise ValueError("displayName must be a nonempty string.")
            if body.get("cost") is not None and _user_rates(body["cost"])[0] is None:
                raise ValueError("Invalid model cost.")
            custom = next(
                (
                    m
                    for m in config.customModels
                    if m["provider"] == provider.id and m["modelId"] == model
                ),
                None,
            )
            new_model = body.get("newModelId", model)
            requested_model = model
            if new_model != model:
                if not custom or not isinstance(new_model, str) or not new_model.strip():
                    raise ValueError("Only custom models can be renamed.")
                cached = inventory.cache.get(provider.id)
                live_ids = (
                    [row["id"] for row in cached[2]]
                    if provider.liveModels
                    and cached
                    and cached[0] == inventory.fingerprint(provider)
                    else []
                )
                static_ids = provider.models if not provider.liveModels else []
                if any(
                    m is not custom
                    and m["provider"] == provider.id
                    and routed_slug(provider.id, m["modelId"])
                    == routed_slug(provider.id, new_model)
                    for m in config.customModels
                ) or any(
                    routed_slug(provider.id, known) == routed_slug(provider.id, new_model)
                    for known in [*static_ids, *live_ids]
                ):
                    raise HTTPException(409, "Duplicate model.")
                for mapping in [
                    *MODEL_FIELDS.values(),
                    "modelCapabilities",
                    "modelCosts",
                    "modelAliases",
                ]:
                    values = dict(getattr(provider, mapping, {}) or {})
                    if model in values:
                        if new_model in values:
                            raise HTTPException(409, "Model settings already exist for this ID.")
                        values[new_model] = values.pop(model)
                        setattr(provider, mapping, values)
                custom["modelId"] = new_model
                provider.selectedModels = [
                    new_model if item == model else item for item in provider.selectedModels
                ]
                provider.models = [new_model if item == model else item for item in provider.models]
                if provider.defaultModel == model:
                    provider.defaultModel = new_model
                if config.defaultProvider == provider.id and config.defaultModel == model:
                    config.defaultModel = new_model
                old_slugs = {routed_slug(provider.id, model), f"{provider.id}/{model}"}
                config.disabledModels = [
                    routed_slug(provider.id, new_model) if item in old_slugs else item
                    for item in config.disabledModels
                ]
                model = new_model
            for field, mapping in maps.items():
                if field not in body:
                    continue
                capabilities = dict(getattr(provider, "modelCapabilities", {}) or {})
                per_model = dict(capabilities.get(model, {}) or {})
                per_model.pop(field, None)
                if per_model:
                    capabilities[model] = per_model
                else:
                    capabilities.pop(model, None)
                if capabilities != (getattr(provider, "modelCapabilities", {}) or {}):
                    provider.modelCapabilities = capabilities
                if custom is not None:
                    values = dict(getattr(provider, mapping, {}) or {})
                    values.pop(model, None)
                    if values != (getattr(provider, mapping, {}) or {}):
                        setattr(provider, mapping, values)
                    if body[field] is None:
                        custom.pop(field, None)
                    else:
                        custom[field] = body[field]
                    continue
                values = dict(getattr(provider, mapping, {}) or {})
                if body[field] is None:
                    values.pop(model, None)
                else:
                    values[model] = body[field]
                if values != (getattr(provider, mapping, {}) or {}):
                    setattr(provider, mapping, values)
            if "cost" in body:
                costs = dict(getattr(provider, "modelCosts", {}) or {})
                if body["cost"] is None:
                    costs.pop(model, None)
                else:
                    costs[model] = body["cost"]
                if costs != (getattr(provider, "modelCosts", {}) or {}):
                    provider.modelCosts = costs
            changed = config.model_dump() != before
            if changed:
                store.save(config)
            overrides = any(
                model in (getattr(provider, mapping, {}) or {})
                for mapping in [*maps.values(), "modelCosts"]
            )
            overrides = overrides or any(
                field in (getattr(provider, "modelCapabilities", {}) or {}).get(model, {})
                for field in maps
            )
            overrides = overrides or bool(custom and any(field in custom for field in maps))
        refresh = (await converge())["catalogRefresh"] if changed else {"status": "skipped"}
        if refresh.get("status") == "pending":
            refresh = {**refresh, "status": "failed", "retryable": True}
        return {
            "ok": True,
            "provider": provider.id,
            "modelId": requested_model,
            "changed": changed,
            "saved": changed,
            "hasOverrides": overrides,
            "catalogRefresh": refresh,
        }

    @router.put("/providers/{name}/alias")
    async def provider_alias(name: str, body: dict):
        alias = body.get("alias")
        if alias and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", alias):
            raise ValueError("Invalid provider alias.")
        with store.mutation_lock:
            config = store.load()
            provider = find(config, name)
            if alias and any(
                p.id != name and alias.lower() in {p.id.lower(), (p.alias or "").lower()}
                for p in config.providers
            ):
                raise ValueError("Provider alias conflicts with another connection.")
            provider.alias = alias or None
            store.save(config)
        return {"ok": True, "alias": alias, **await converge()}

    @router.put("/providers/{name}/model-aliases")
    @router.put("/providers/{name}/model-display-names")
    async def model_labels(name: str, body: dict, request: Request):
        field = (
            "modelAliases" if request.url.path.endswith("model-aliases") else "modelDisplayNames"
        )
        with store.mutation_lock:
            config = store.load()
            provider = find(config, name)
            values = dict(getattr(provider, field, {}) or {})
            for key in body.get("remove", []):
                values.pop(key, None)
            for key, value in body.get("set", {}).items():
                if not isinstance(value, str) or not value.strip() or "/" in value:
                    raise ValueError("Invalid model label.")
                if field == "modelAliases" and any(
                    k != key and v.lower() == value.lower() for k, v in values.items()
                ):
                    raise ValueError("Model alias collision.")
                values[key] = value.strip()
            setattr(provider, field, values)
            store.save(config)
        return {"ok": True, "aliases": values, **await converge()}

    @router.get("/custom-models")
    def custom_models():
        return store.load().customModels

    def custom_fields(body):
        allowed = {
            "modelId",
            "displayName",
            "contextWindow",
            "inputModalities",
            "reasoningEfforts",
            "defaultReasoningEffort",
        }
        values = {k: v for k, v in body.items() if k in allowed and v is not None}
        if "inputModalities" in values and any(
            m not in {"text", "image", "audio"} for m in values["inputModalities"]
        ):
            raise ValueError("Unsupported input modality.")
        if "contextWindow" in values and (
            not isinstance(values["contextWindow"], int) or values["contextWindow"] <= 0
        ):
            raise ValueError("contextWindow must be positive.")
        return values

    @router.post("/custom-models", status_code=201)
    async def add_custom(body: dict):
        with store.mutation_lock:
            config = store.load()
            provider = find(config, body.get("provider"))
            fields = custom_fields(body)
            model = fields.get("modelId")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("modelId is required.")
            if any(
                m["provider"] == provider.id
                and routed_slug(provider.id, m["modelId"]) == routed_slug(provider.id, model)
                for m in config.customModels
            ):
                raise HTTPException(409, "Duplicate model.")
            entry = {
                "id": str(uuid4()),
                "provider": provider.id,
                **fields,
                "addedAt": datetime.now(UTC).isoformat(),
            }
            config.customModels.append(entry)
            store.save(config)
        return {**entry, **await converge()}

    @router.put("/custom-models/{id}")
    async def edit_custom(id: str, body: dict):
        with store.mutation_lock:
            config = store.load()
            entry = next((m for m in config.customModels if m["id"] == id), None)
            if entry is None:
                raise HTTPException(404, "Model not found.")
            entry.update(custom_fields(body))
            for key, value in body.items():
                if value is None and key not in {"id", "provider", "modelId"}:
                    entry.pop(key, None)
            store.save(config)
        return {**entry, **await converge()}

    @router.delete("/custom-models/{id}")
    async def delete_custom(id: str):
        with store.mutation_lock:
            config = store.load()
            config.customModels = [m for m in config.customModels if m["id"] != id]
            store.save(config)
        return {"ok": True, **await converge()}

    @router.get("/accounts/events")
    async def events(request: Request):
        async def stream():
            revisions = subscriptions.revisions()
            yield f'event: ready\ndata: {{"revision":{max(revisions.values(), default=0)}}}\n\n'
            while not stopping.is_set() and not await request.is_disconnected():
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=1)
                    break
                except TimeoutError:
                    pass
                changed = False
                for name, revision in subscriptions.revisions().items():
                    if revision != revisions[name]:
                        changed = True
                        revisions[name] = revision
                        yield f'event: account-selection\ndata: {{"provider":"{name}","kind":"oauth","revision":{revision}}}\n\n'
                if not changed:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    router.include_router(model_policy_router(store, inventory, converge))
    router.include_router(oauth_router(store, subscriptions, quota, kimi_quota))
    return router
