"""Original model-policy endpoints, separated from provider credential management."""

from fastapi import APIRouter, HTTPException

from .model_policy import DEFAULT_CONTEXT_CAP, PRESETS, effective_aliases, materialize_preset


def model_policy_router(store, inventory, converge):
    router = APIRouter()

    def find(config, name):
        provider = next((p for p in config.providers if p.id == name), None)
        if provider is None:
            raise HTTPException(404, "Provider not found.")
        return provider

    @router.get("/aliases")
    async def aliases():
        rows = await inventory.rows()
        config = store.load()
        return {
            "providers": {p.id: p.alias for p in config.providers if p.alias},
            "models": {
                p.id: {
                    model: {
                        **row,
                        **(
                            {"stale": True}
                            if model not in {r["id"] for r in rows if r["provider"] == p.id}
                            else {}
                        ),
                    }
                    for model, row in effective_aliases(
                        config,
                        p,
                        {r["id"] for r in rows if r["provider"] == p.id} | set(p.modelAliases),
                    ).items()
                }
                for p in config.providers
            },
            "defaults": {
                "global": config.defaultModelAliases,
                "providers": {
                    p.id: p.defaultAliases for p in config.providers if hasattr(p, "defaultAliases")
                },
            },
        }

    @router.put("/default-aliases")
    async def default_aliases(body: dict):
        if not isinstance(body.get("enabled"), bool):
            raise HTTPException(400, "enabled must be boolean")
        with store.mutation_lock:
            config = store.load()
            if "provider" in body:
                find(config, body["provider"]).defaultAliases = body["enabled"]
            else:
                config.defaultModelAliases = body["enabled"]
            store.save(config)
        return {"ok": True, **await converge()}

    @router.get("/model-presets")
    async def presets():
        rows = await inventory.rows()
        providers = {}
        for provider in store.load().providers:
            if provider.id not in PRESETS:
                continue
            ids = [r["id"] for r in rows if r["provider"] == provider.id]
            selected = materialize_preset(provider.id, ids)
            marker = getattr(provider, "modelPreset", {})
            providers[provider.id] = {
                "mode": marker.get("mode", "all"),
                "availableVersion": 1,
                "presetIds": selected,
                "presetCount": len(selected),
                "totalCount": len(ids),
                **{key: marker[key] for key in ("appliedVersion", "fallback") if key in marker},
            }
        return {"providers": providers}

    @router.put("/model-presets")
    async def apply_preset(body: dict):
        mode, name = body.get("mode"), body.get("provider")
        if mode not in {"preset", "all", "custom"}:
            raise ValueError("mode must be preset, all, or custom")
        if mode == "preset" and name not in PRESETS:
            raise ValueError("No model preset is shipped for this provider.")
        rows = await inventory.rows()
        with store.mutation_lock:
            config = store.load()
            provider = find(config, name)
            if getattr(provider, "initialModelSelection", {}).get("status") == "pending":
                raise HTTPException(409, "Initial model discovery is pending. Refresh and retry.")
            marker = dict(getattr(provider, "modelPreset", {}))
            if mode == "all":
                provider.selectedModels = []
                provider.model_extra.pop("modelPreset", None)
            elif mode == "custom":
                provider.modelPreset = {**marker, "mode": "custom"}
            else:
                selected = materialize_preset(
                    name, [r["id"] for r in rows if r["provider"] == name]
                )
                provider.modelPreset = {
                    "mode": "preset" if selected else "all",
                    "appliedVersion": 1,
                }
                if selected:
                    provider.selectedModels = selected
                else:
                    provider.modelPreset["fallback"] = "preset-empty"
            store.save(config)
        return {
            "ok": True,
            "provider": name,
            "mode": provider.modelPreset["mode"] if mode == "preset" else mode,
            **(
                {"fallback": "preset-empty"}
                if mode == "preset" and provider.modelPreset.get("fallback") == "preset-empty"
                else {}
            ),
            "selected": provider.selectedModels,
            **await converge(),
        }

    @router.get("/model-discovery")
    def discovery():
        config = store.load()
        state = getattr(config, "modelDiscovery", {})
        return {
            "policy": state.get("newModelPolicy", "on"),
            "providers": {p.id: getattr(p, "newModelPolicy", "inherit") for p in config.providers},
            "recentArrivals": {
                name: [
                    {
                        **r,
                        "state": "auto-disabled"
                        if f"{name}/{r['id'].replace('/', '-')}" in config.disabledModels
                        else "enabled",
                    }
                    for r in rows
                ]
                for name, rows in state.get("recentArrivals", {}).items()
            },
            "baselineCounts": {
                name: len(value["ids"]) for name, value in state.get("knownModels", {}).items()
            },
        }

    @router.put("/model-discovery")
    async def set_discovery(body: dict):
        policy = body.get("policy")
        if policy not in {"on", "off"}:
            raise ValueError("policy must be on or off")
        # Capture a baseline before turning arrivals off; existing models remain unchanged.
        await inventory.rows()
        with store.mutation_lock:
            config = store.load()
            if body.get("provider"):
                find(config, body["provider"]).newModelPolicy = policy
            else:
                config.modelDiscovery = {
                    **getattr(config, "modelDiscovery", {}),
                    "newModelPolicy": policy,
                }
            store.save(config)
        return {"ok": True, "policy": policy, "provider": body.get("provider"), **await converge()}

    @router.post("/model-discovery/acknowledge")
    async def acknowledge(body: dict):
        ids = body.get("ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError("provider and string ids are required")
        with store.mutation_lock:
            config = store.load()
            name = find(config, body.get("provider")).id
            state = dict(getattr(config, "modelDiscovery", {}))
            recent = dict(state.get("recentArrivals", {}))
            recent[name] = [r for r in recent.get(name, []) if r["id"] not in ids]
            config.modelDiscovery = {**state, "recentArrivals": recent}
            store.save(config)
        return {
            "ok": True,
            "provider": name,
            "acknowledged": list(dict.fromkeys(ids)),
            **await converge(),
        }

    @router.get("/provider-context-caps")
    def caps():
        config = store.load()
        active = getattr(config, "providerContextCaps", {})
        return {
            "cap": DEFAULT_CONTEXT_CAP,
            "value": getattr(config, "contextCapValue", DEFAULT_CONTEXT_CAP),
            "caps": active,
            "values": {**getattr(config, "providerContextCapValues", {}), **active},
        }

    @router.put("/provider-context-caps")
    async def set_caps(body: dict):
        value = body.get("value")
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 1 <= value < float("inf")
        ):
            raise ValueError("value must be a positive finite number")
        value = int(value) if value is not None else None
        with store.mutation_lock:
            config = store.load()
            active = dict(getattr(config, "providerContextCaps", {}))
            remembered = {**getattr(config, "providerContextCapValues", {}), **active}
            default = getattr(config, "contextCapValue", DEFAULT_CONTEXT_CAP)
            if "provider" in body or "enabled" in body:
                if (
                    not isinstance(body.get("enabled"), bool)
                    or not isinstance(body.get("provider"), str)
                    or "setAll" in body
                ):
                    raise ValueError("provider and enabled are required together")
                name = find(config, body["provider"]).id
                if body["enabled"]:
                    active[name] = value or remembered.get(name, default)
                    remembered[name] = active[name]
                else:
                    active.pop(name, None)
            elif value is not None:
                if "setAll" in body and not isinstance(body["setAll"], bool):
                    raise ValueError("setAll must be boolean")
                config.contextCapValue = value
                if body.get("setAll"):
                    active = dict.fromkeys(active, value)
                    remembered.update(active)
            elif isinstance(body.get("setAll"), bool):
                active = {p.id: default for p in config.providers} if body["setAll"] else {}
                remembered.update(active)
            else:
                raise ValueError("provider toggle, value, or setAll required")
            config.providerContextCaps = active
            config.providerContextCapValues = remembered
            store.save(config)
        return {"ok": True, **caps(), **await converge()}

    return router
