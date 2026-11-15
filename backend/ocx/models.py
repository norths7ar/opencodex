"""Model inventory shared by the original console, gateway and Codex catalog."""

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import time
from datetime import UTC, datetime

import httpx

from .config import ConfigStore, Provider
from .model_policy import reconcile_discovery
from .official_model_metadata import OfficialModelMetadata
from .provider_registry import REGISTRY
from .usage_pricing import RATE_KEYS, _bundled_price, _rates, _user_rates, pricing_family

MODEL_FIELDS = {
    "contextWindow": "modelContextWindows",
    "inputModalities": "modelInputModalities",
    "outputModalities": "modelOutputModalities",
    "apiCapabilities": "modelApiCapabilities",
    "reasoningEfforts": "modelReasoningEfforts",
    "defaultReasoningEffort": "modelDefaultReasoningEfforts",
    "displayName": "modelDisplayNames",
    "parallelToolCalls": "modelParallelToolCalls",
    "codexToolMode": "modelCodexToolModes",
    "maxInputTokens": "modelMaxInputTokens",
    "maxOutputTokens": "modelMaxOutputTokens",
    "autoCompactTokenLimit": "modelAutoCompactTokenLimits",
    "supportsReasoningSummaries": "modelSupportsReasoningSummaries",
    "supportsVerbosity": "modelSupportsVerbosity",
    "suppressSyntheticMax": "modelSuppressSyntheticMax",
    "supportsServiceTier": "modelSupportsServiceTier",
    "supportsReasoning": "modelSupportsReasoning",
    "supportsReasoningEffort": "modelSupportsReasoningEffort",
    "supportsThinkingToggle": "modelSupportsThinkingToggle",
    "defaultThinkingEnabled": "modelDefaultThinkingEnabled",
}


def matching_seed(data: dict) -> dict:
    for seed in REGISTRY["seeds"].values():
        if seed.get("adapter") == data.get("adapter") and seed.get("baseUrl", "").rstrip(
            "/"
        ) == data.get("baseUrl", "").rstrip("/"):
            return seed
    return {}


def preset_seed(data: dict) -> dict:
    """Seed connection defaults; live rosters and metadata retain their own provenance."""
    seed = matching_seed(data)
    inherited = {**seed}
    for field in {*MODEL_FIELDS, *MODEL_FIELDS.values(), "modelCapabilities"}:
        inherited.pop(field, None)
    if data.get("liveModels", seed.get("liveModels", True)):
        inherited.pop("models", None)
    return {**inherited, "presetMetadataVersion": 2, **data}


def separate_preset_metadata(provider: Provider) -> bool:
    """Separate legacy seed copies once, so subsequent edits have explicit provenance.

    Older configurations did not record origin. Values identical to their matched seed
    are treated as inherited; differing values and custom-model declarations are retained.
    """
    if getattr(provider, "presetMetadataVersion", None) == 2:
        return False
    seed = matching_seed(provider.model_dump())
    extra = provider.model_extra
    if seed:
        for field in MODEL_FIELDS:
            if field in extra and extra[field] == seed.get(field):
                extra.pop(field)
        for mapping in {*MODEL_FIELDS.values(), "modelCapabilities"}:
            if mapping not in extra:
                continue
            inherited = seed.get(mapping, {})
            extra[mapping] = {
                model: value
                for model, value in extra[mapping].items()
                if model not in inherited or value != inherited[model]
            }
            if not extra[mapping]:
                extra.pop(mapping)
        if provider.liveModels and provider.models == seed.get("models"):
            provider.models = []
    provider.presetMetadataVersion = 2
    return True


def upstream_metadata(model: dict) -> dict:
    """Normalize live protocol fields without requiring a locally known model ID."""
    result = {field: model[field] for field in MODEL_FIELDS if field in model}
    for source, target in {
        "name": "displayName",
        "display_name": "displayName",
        "context_window": "contextWindow",
        "max_input_tokens": "maxInputTokens",
        "max_output_tokens": "maxOutputTokens",
        "input_modalities": "inputModalities",
        "output_modalities": "outputModalities",
        "api_capabilities": "apiCapabilities",
        "supports_parallel_tool_calls": "parallelToolCalls",
        "default_reasoning_level": "defaultReasoningEffort",
    }.items():
        if model.get(source) is not None:
            result[target] = model[source]
    effort = model.get("effort")
    if isinstance(effort, dict):
        if isinstance(effort.get("supported_levels"), list):
            result["reasoningEfforts"] = effort["supported_levels"]
        if effort.get("default_level"):
            result["defaultReasoningEffort"] = effort["default_level"]
    levels = model.get("supported_reasoning_levels")
    if isinstance(levels, list):
        result["reasoningEfforts"] = [
            level["effort"] for level in levels if isinstance(level, dict) and level.get("effort")
        ]
    for field in ("contextWindow", "maxInputTokens", "maxOutputTokens"):
        value = result.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            result.pop(field)
    for field in ("inputModalities", "outputModalities", "reasoningEfforts"):
        value = result.get(field)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            result.pop(field)
    if "apiCapabilities" in result and not isinstance(result["apiCapabilities"], dict):
        result.pop("apiCapabilities")
    return result


def model_metadata(data: dict, seed: dict, model: str, live: dict, custom: dict, official=None):
    """Operator declarations override live values; presets only fill missing metadata."""
    missing = object()
    result = {}
    sources = {}
    official = official or {}

    def value(source, field, mapping, *, include_global=True):
        return (
            source.get("modelCapabilities", {})
            .get(model, {})
            .get(
                field,
                source.get(mapping, {}).get(
                    model, source.get(field, missing) if include_global else missing
                ),
            )
        )

    for field, mapping in MODEL_FIELDS.items():
        fallback = value(seed, field, mapping)
        configured = value(data, field, mapping, include_global=False)
        if field in custom:
            chosen = custom[field]
            source = {"source": "operator"}
        elif configured is not missing and (
            data.get("presetMetadataVersion") == 2 or configured != fallback
        ):
            chosen = configured
            source = {"source": "operator"}
        elif field in data:
            chosen = data[field]
            source = {"source": "provider"}
        elif field in live:
            chosen = live[field]
            source = {"source": "upstream"}
        elif field in official:
            chosen = official[field]
            source = official.get("provenance", {"source": "official"})
        else:
            chosen = fallback if fallback is not missing else configured
            source = {"source": "preset"}
        if chosen is not missing and chosen is not None:
            result[field] = chosen
            sources[field] = source
    return result, sources


def routed_slug(provider: str, model: str) -> str:
    return f"{provider}/{model.replace('/', '-')}"


class ModelInventory:
    def __init__(self, store: ConfigStore, fetch_models, auth_revision):
        self.store = store
        self.fetch_models = fetch_models
        self.auth_revision = auth_revision
        self.cache: dict[str, tuple[str, float, list[dict]]] = {}
        self.discovery: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.official = OfficialModelMetadata(store.directory)
        self.path = store.directory / "model-catalog.json"
        self.wake = asyncio.Event()
        self._load_cache()

    def _load_cache(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("version") != 1:
                return
            for name, value in saved["providers"].items():
                rows = value["models"]
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows
                ):
                    continue
                self.cache[name] = (value["fingerprint"], float(value["checked"]), rows)
                self.discovery[name] = value["discovery"]
        except OSError, ValueError, KeyError, TypeError:
            self.cache.clear()
            self.discovery.clear()

    def _save_cache(self):
        names = {p.id for p in self.store.load().providers}
        saved = {
            "version": 1,
            "providers": {
                name: {
                    "fingerprint": fingerprint,
                    "checked": checked,
                    "models": rows,
                    "discovery": self.discovery.get(name, {}),
                }
                for name, (fingerprint, checked, rows) in self.cache.items()
                if name in names
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(saved), encoding="utf-8")
        temporary.replace(self.path)

    async def refresh_due(self):
        providers = [p for p in self.store.load().providers if not p.disabled]
        await asyncio.gather(*(self.discover(provider) for provider in providers))
        await asyncio.gather(*(self.official.read(provider) for provider in providers))

    async def run(self, on_refresh):
        """Own discovery independently of reads and page polling."""
        while True:
            self.wake.clear()
            try:
                before = dict(self.cache)
                metadata_before = copy.deepcopy(self.official.models)
                await self.refresh_due()
                if self.cache != before or self.official.models != metadata_before:
                    await on_refresh()
            except OSError, ValueError, RuntimeError:
                logging.getLogger(__name__).exception("Model directory refresh failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=60)

    def invalidate(self, provider: str | None = None, *, reset=False, revalidate=False):
        # Configuration-only changes do not re-query an unchanged connection.
        providers = {p.id: p for p in self.store.load().providers}
        for name in [provider] if provider else list(self.cache):
            current = providers.get(name)
            cached = self.cache.get(name)
            if reset or current is None or (cached and cached[0] != self.fingerprint(current)):
                self.cache.pop(name, None)
                self.discovery.pop(name, None)
            elif revalidate and cached:
                self.cache[name] = (cached[0], 0, cached[2])
            self.store.discovered_models.pop(name, None)
        self._save_cache()
        self.wake.set()

    def fingerprint(self, provider: Provider) -> str:
        credential = provider.protectedKey or provider.apiKeyEnv
        if provider.authMode in {"key", "local"}:
            with contextlib.suppress(ValueError, RuntimeError):
                credential = self.store.credential(provider)
        values = (
            provider.baseUrl,
            provider.adapter,
            provider.authMode,
            credential,
            provider.headers,
            provider.apiKeyTransport,
            provider.responsesPath,
            self.auth_revision(provider) if provider.authMode == "oauth" else None,
        )
        return hashlib.sha256(repr(values).encode()).hexdigest()

    async def discover(self, provider: Provider, *, force=False) -> list[dict]:
        if provider.disabled or not provider.liveModels:
            return []
        fingerprint = self.fingerprint(provider)
        started = time.time()
        async with self.locks.setdefault(provider.id, asyncio.Lock()):
            cached = self.cache.get(provider.id)
            previous = (
                self.discovery.get(provider.id, {}) if cached and cached[0] == fingerprint else {}
            )
            ttl = 86400
            if (
                cached
                and cached[0] == fingerprint
                and (not force or cached[1] >= started)
                and time.time() - cached[1] < ttl
            ):
                return cached[2]
            checked_at = datetime.now(UTC).isoformat()
            try:
                rows = await self.fetch_models(provider)
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not row["id"].strip()
                    for row in rows
                ):
                    raise ValueError("Upstream did not return a valid model directory.")
                current = next(
                    (p for p in self.store.load().providers if p.id == provider.id), None
                )
                if current is None or self.fingerprint(current) != fingerprint:
                    self.wake.set()
                    return []
                self.cache[provider.id] = (fingerprint, time.time(), rows)
                self.discovery[provider.id] = {
                    "status": "ok",
                    "checkedAt": checked_at,
                    "lastSuccessAt": checked_at,
                }
                self.reconcile(provider, [row["id"] for row in rows])
                self._save_cache()
                await self.official.read(provider, force=force)
                return rows
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                current = next(
                    (p for p in self.store.load().providers if p.id == provider.id), None
                )
                if current is None or self.fingerprint(current) != fingerprint:
                    self.wake.set()
                    return []
                self.discovery[provider.id] = {
                    "status": "failed",
                    "reason": "network" if isinstance(exc, httpx.HTTPError) else "invalid_response",
                    "checkedAt": checked_at,
                    "lastSuccessAt": previous.get("lastSuccessAt"),
                    "error": str(exc),
                }
                # Retain the last known inventory on temporary discovery failure.
                rows = cached[2] if cached and cached[0] == fingerprint else []
                self.cache[provider.id] = (fingerprint, time.time(), rows)
                self._save_cache()
                return rows

    def reconcile(self, observed, ids):
        with self.store.mutation_lock:
            config = self.store.load()
            current = next((p for p in config.providers if p.id == observed.id), None)
            if current is None or self.fingerprint(current) != self.fingerprint(observed):
                return
            separated = separate_preset_metadata(current)
            if reconcile_discovery(config, current, ids) or separated:
                self.store.save(config)

    async def rows(self) -> list[dict]:
        config = self.store.load()
        fingerprints = {p.id: self.fingerprint(p) for p in config.providers}
        live = [
            cached[2]
            if p.liveModels
            and not p.disabled
            and (cached := self.cache.get(p.id))
            and cached[0] == fingerprints[p.id]
            else []
            for p in config.providers
        ]
        official_rows = [self.official.cached(p) for p in config.providers]
        official_by_name = {
            p.id: rows for p, rows in zip(config.providers, official_rows, strict=True)
        }
        discovered_by_name = {p.id: rows for p, rows in zip(config.providers, live, strict=True)}
        for provider in config.providers:
            if not provider.liveModels:
                self.reconcile(provider, provider.models)
        config = self.store.load()
        result = []
        for provider in config.providers:
            discovered = discovered_by_name.get(provider.id, [])
            if fingerprints.get(provider.id) != self.fingerprint(provider):
                discovered = []
                official_by_name.pop(provider.id, None)
                cached = self.cache.get(provider.id)
                if cached and cached[0] != self.fingerprint(provider):
                    self.discovery.pop(provider.id, None)
            data = provider.model_dump()
            seed = matching_seed(data)
            custom = {m["modelId"]: m for m in config.customModels if m["provider"] == provider.id}
            live_by_id = {m["id"]: m for m in discovered}
            known = dict.fromkeys(
                [*(live_by_id if provider.liveModels else provider.models), *custom]
            )
            status = (
                self.discovery.get(provider.id)
                if fingerprints.get(provider.id) == self.fingerprint(provider)
                else None
            )
            if status is not None:
                default = provider.defaultModel or (
                    config.defaultModel if config.defaultProvider == provider.id else None
                )
                status["staleDefaultModel"] = (
                    default
                    if status["status"] == "ok" and default and default not in known
                    else None
                )
                status["staleAliases"] = (
                    {
                        model: alias
                        for model, alias in provider.modelAliases.items()
                        if model not in known
                    }
                    if status["status"] == "ok"
                    else {}
                )
            # Route resolution needs exact native IDs to decode one-slash Codex selectors.
            self.store.discovered_models[provider.id] = list(known)
            for model in known:
                declaration = custom.get(model, {})
                live = upstream_metadata(live_by_id.get(model, {}))
                official = official_by_name.get(provider.id, {}).get(model, {})
                metadata, sources = model_metadata(data, seed, model, live, declaration, official)
                inherited_data = {field: data[field] for field in MODEL_FIELDS if field in data}
                inherited, _ = model_metadata(inherited_data, seed, model, live, {}, official)
                for source in sources.values():
                    if source.get("source") == "upstream":
                        source["updatedAt"] = self.discovery.get(provider.id, {}).get(
                            "lastSuccessAt"
                        )
                        source["stale"] = (
                            self.discovery.get(provider.id, {}).get("status") == "failed"
                        )
                overrides = {
                    field: metadata[field]
                    for field in MODEL_FIELDS
                    if sources.get(field, {}).get("source") == "operator" and field in metadata
                }
                cost_override = data.get("modelCosts", {}).get(model)
                cost_rates, _ = _user_rates(cost_override)
                price = live_by_id.get(model, {}).get("pricing")
                upstream_rates = (
                    _rates(price)
                    if isinstance(price, dict)
                    and price.get("currency") == "USD"
                    and price.get("unit") == "per_million_tokens"
                    else None
                )
                bundled = (
                    None
                    if official.get("pricing")
                    else _bundled_price(pricing_family(provider), model)
                )
                if upstream_rates is not None:
                    bundled = (
                        upstream_rates,
                        {
                            "source": "upstream",
                            "updatedAt": self.discovery.get(provider.id, {}).get("lastSuccessAt"),
                            "stale": self.discovery.get(provider.id, {}).get("status") == "failed",
                        },
                    )
                if bundled:
                    rates, provenance = bundled
                    inherited["effectiveCost"] = dict(zip(RATE_KEYS, rates, strict=True))
                else:
                    provenance = None
                if cost_rates is not None:
                    effective_cost = cost_override
                    sources["effectiveCost"] = {"source": "operator"}
                    overrides["effectiveCost"] = cost_override
                else:
                    effective_cost = inherited.get("effectiveCost")
                    if provenance:
                        sources["effectiveCost"] = {
                            **provenance,
                            "source": "upstream" if upstream_rates is not None else "preset",
                        }
                slug = routed_slug(provider.id, model)
                selected = not provider.selectedModels or model in provider.selectedModels
                disabled = (
                    provider.disabled
                    or getattr(provider, "initialModelSelection", {}).get("status") == "pending"
                    or slug in config.disabledModels
                    or f"{provider.id}/{model}" in config.disabledModels
                )
                context = metadata.get("contextWindow")
                row = {
                    **metadata,
                    "provider": provider.id,
                    "id": model,
                    "namespaced": slug,
                    "disabled": disabled,
                    "selected": selected,
                    "subscription": provider.authMode == "oauth",
                    "metadataSources": sources,
                    "metadataOverrides": overrides,
                    "inheritedMetadata": inherited,
                }
                if effective_cost is not None:
                    row["effectiveCost"] = effective_cost
                if cost_rates is not None:
                    row["costOverride"] = cost_override
                if official.get("pricing"):
                    row["pricingSchedule"] = {
                        **official["pricing"],
                        "stale": official.get("provenance", {}).get("stale", False),
                    }
                cap = (config.model_extra or {}).get("providerContextCaps", {}).get(provider.id)
                if cap:
                    row.update(contextCap=cap, contextCapped=bool(context and context > cap))
                    row["contextWindow"] = min(context, cap) if context else cap
                if getattr(provider, "initialModelSelection", {}).get("status") == "pending":
                    row["initialSelectionPending"] = True
                if model in data.get("modelContextWindows", {}):
                    row["contextWindowDeclared"] = data["modelContextWindows"][model]
                if model in data.get("modelInputModalities", {}):
                    row["inputModalitiesDeclared"] = data["modelInputModalities"][model]
                display = data.get("modelDisplayNames", {}).get(model)
                row.update(
                    displayName=metadata.get("displayName") or model,
                    displayNameSource=sources.get("displayName", {}).get("source", "fallback"),
                )
                if display or declaration.get("displayName"):
                    row["displayNameOverride"] = display or declaration["displayName"]
                if declaration:
                    row.update(custom=True, customId=declaration["id"])
                result.append(row)
        return result

    async def selection(self):
        rows = await self.rows()
        config = self.store.load()
        return {
            "selected": {p.id: p.selectedModels for p in config.providers if p.selectedModels},
            "available": {
                p.id: [r["id"] for r in rows if r["provider"] == p.id] for p in config.providers
            },
            "liveModelCounts": {name: len(cache[2]) for name, cache in self.cache.items()},
            "discovery": self.discovery,
        }
