"""Modern Subagents GUI contracts over the gateway's catalog and selected client."""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException

from . import client_catalog, client_process, clients
from .subagent_client import supports_mode_hint
from .subagent_fallback import SubagentFallback
from .subagent_sizing import suggest
from .subagents import EFFORTS, MODE_HINT, apply_guidance, feature_state, setting


class SubagentService:
    def __init__(self, store, inventory, integration):
        self.store = store
        self.inventory = inventory
        self.integration = integration
        self._freshness = (0, {"state": "unknown", "runningCount": 0})
        self.complete_sizing = None
        self.fallback = SubagentFallback(store, integration)

    async def catalog(self):
        rows = await self.inventory.rows()
        catalog, _ = client_catalog.prepare_catalog(
            self.integration.home(), self.integration.directory, rows, config=self.store.load()
        )
        return catalog["models"]

    async def catalog_state(self):
        now = time.monotonic()
        if now - self._freshness[0] > 5:
            path = client_catalog.catalog_path(self.integration.home(), self.integration.directory)
            value = await asyncio.to_thread(client_process.freshness, path)
            self._freshness = (now, value)
        return self._freshness[1]

    async def prepare_request(self, body):
        """Gateway hook: call on Responses bodies before native/translated dispatch."""
        if not self.integration.record().get("enabled"):
            return False
        path = client_catalog.catalog_path(self.integration.home(), self.integration.directory)
        raw = clients._read(path, 32 << 20, "Codex model catalog")
        if raw is None:
            return False
        entries = json.loads(raw)["models"]
        state = await self.catalog_state()
        applied = apply_guidance(body, self.store.load(), entries, catalog_state=state["state"])
        logging.getLogger("ocx.subagents").debug(
            "Delegation guidance applied=%s catalog_state=%s catalog_models=%d",
            applied,
            state["state"],
            len(entries),
            extra={"ocx_debug_stream": "injection"},
        )
        return applied

    async def converge(self):
        self._freshness = (0, {"state": "unknown", "runningCount": 0})
        return await self.integration.sync()


def subagent_router(service: SubagentService):
    router = APIRouter(prefix="/api")
    store, integration = service.store, service.integration

    def update(patch):
        with store.mutation_lock:
            config = store.load()
            for key, value in patch.items():
                if value is None:
                    config.model_extra.pop(key, None)
                else:
                    setattr(config, key, value)
            store.save(config)

    @router.get("/subagent-models")
    async def roster():
        entries = await service.catalog()
        config = store.load()
        chosen = setting(config, "subagentModels", [])
        available = list(dict.fromkeys([entry["slug"] for entry in entries] + chosen))
        return {
            "chosen": chosen,
            "available": available,
            "catalogState": await service.catalog_state(),
            "force": None,
            "forceAvailable": [],
            "forceStatus": None,
            "pickerAvailable": [
                entry["slug"] for entry in entries if entry.get("visibility") == "list"
            ],
            "pickerOrder": setting(config, "modelPickerOrder", []),
            "pickerOrderMode": setting(config, "modelPickerOrderMode"),
        }

    @router.put("/subagent-models")
    async def save_roster(body: dict):
        patch = {}
        if "force" in body:
            raise HTTPException(
                400, "Claude Code subagent pinning is not enabled for this installation."
            )
        if "models" in body:
            if not isinstance(body["models"], list) or any(
                not isinstance(v, str) for v in body["models"]
            ):
                raise HTTPException(400, "models must be an array of strings")
            patch["subagentModels"] = body["models"][:5]
        if "pickerOrder" in body:
            order = body["pickerOrder"]
            if order is not None and (
                not isinstance(order, list)
                or any(not isinstance(v, str) or not v.strip() for v in order)
            ):
                raise HTTPException(
                    400, "pickerOrder must be an array of non-empty model IDs or null"
                )
            order = [v.strip() for v in order or []]
            visible = {e["slug"] for e in await service.catalog() if e.get("visibility") == "list"}
            if len(order) != len(set(order)) or not set(order) <= visible:
                raise HTTPException(400, "pickerOrder must contain unique visible model IDs")
            mode = body.get("pickerOrderMode")
            if mode not in {None, "alphabetical", "provider", "most-used"}:
                raise HTTPException(400, "Unknown picker order mode")
            patch.update(
                modelPickerOrder=order or None, modelPickerOrderMode=mode if order else None
            )
        elif "pickerOrderMode" in body:
            raise HTTPException(400, "pickerOrderMode requires pickerOrder")
        if not patch:
            raise HTTPException(400, "models or pickerOrder is required")
        update(patch)
        refresh = await service.converge()
        value = await roster()
        return {
            "ok": True,
            "applied": value["chosen"],
            "pickerOrder": value["pickerOrder"],
            "pickerOrderMode": value["pickerOrderMode"],
            "catalogRefresh": refresh,
        }

    @router.get("/injection-model")
    async def delegation():
        config = store.load()
        available = []
        for entry in await service.catalog():
            slug = entry["slug"]
            provider, _, model = slug.partition("/")
            available.append(
                {
                    "provider": provider if model else "openai",
                    "model": model or slug,
                    "namespaced": slug,
                }
            )
        return {
            "multiAgentGuidanceEnabled": setting(config, "multiAgentGuidanceEnabled", True),
            "syncCodexSubagentDefaults": bool(
                setting(config, "syncCodexSubagentDefaults") and setting(config, "injectionModel")
            ),
            "model": setting(config, "injectionModel"),
            "effort": setting(config, "injectionEffort"),
            "prompt": setting(config, "injectionPrompt"),
            "efforts": EFFORTS,
            "available": available,
        }

    @router.put("/injection-model")
    async def save_delegation(body: dict):
        config = store.load()
        patch = {}
        for key in ("multiAgentGuidanceEnabled", "syncCodexSubagentDefaults"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise HTTPException(400, f"{key} must be a boolean")
                patch[key] = body[key]
        for key, field in {
            "model": "injectionModel",
            "effort": "injectionEffort",
            "prompt": "injectionPrompt",
        }.items():
            if key in body:
                value = body[key]
                if value is not None and (
                    not isinstance(value, str) or value and not value.strip()
                ):
                    raise HTTPException(400, f"{key} must be a nonblank string or null")
                if key == "effort" and value and value not in EFFORTS:
                    raise HTTPException(400, "Unknown reasoning effort")
                patch[field] = value or None
        model = patch.get("injectionModel", setting(config, "injectionModel"))
        if not model:
            if body.get("syncCodexSubagentDefaults") is True:
                raise HTTPException(400, "Native defaults require an injection model")
            patch.update(injectionEffort=None, syncCodexSubagentDefaults=None)
        update(patch)
        await service.converge()
        return {"ok": True, **await delegation()}

    @router.post("/injection-model/suggest")
    async def suggest_delegation(body: dict):
        work, model = body.get("work"), body.get("model")
        if not isinstance(work, str) or not work.strip() or len(work) > 1500:
            raise HTTPException(400, "work must be a nonblank string of at most 1500 characters")
        if model is not None and not isinstance(model, str):
            raise HTTPException(400, "model must be a string")
        if not model or not model.strip():
            document = clients._parse(
                clients._read(integration.home() / "config.toml", 1 << 20, "Codex config.toml")
            )
            model = document.get("model")
        if not model:
            raise HTTPException(409, "No default model is set in Codex; select a sizing model.")
        if service.complete_sizing is None:
            raise HTTPException(503, "The gateway sizing transport is unavailable.")
        try:
            async with asyncio.timeout(180):
                return await suggest(
                    work.strip(),
                    model.strip(),
                    store.load(),
                    await service.catalog(),
                    store,
                    service.complete_sizing,
                )
        except TimeoutError as exc:
            raise HTTPException(
                504, "The model recommendation timed out; retry when available."
            ) from exc

    @router.get("/v2")
    def mode_state():
        config = store.load()
        state = feature_state(integration.home())
        return {
            **state,
            "multiAgentMode": setting(config, "multiAgentMode", "default"),
            "keepNativeChatGptOnV1": setting(config, "keepNativeChatGptOnV1", False),
            "multiAgentModeHintRecommendation": MODE_HINT,
            "agentsMaxThreadsConflict": False,
            "agentsMaxDepthAppliesWhenV2Disabled": not state["enabled"],
            "pendingClientApply": not integration.record().get("enabled", False),
            "configured": setting(config, "codexAgentSettings", {}),
        }

    @router.put("/v2")
    async def save_mode(body: dict):
        config = store.load()
        patch = {}
        mode = body.get("multiAgentMode", setting(config, "multiAgentMode", "default"))
        if mode not in {"v1", "default", "v2"}:
            raise HTTPException(400, "multiAgentMode must be v1, default, or v2")
        if "multiAgentMode" in body:
            patch["multiAgentMode"] = mode
        keep = body.get("keepNativeChatGptOnV1", setting(config, "keepNativeChatGptOnV1", False))
        if not isinstance(keep, bool):
            raise HTTPException(400, "keepNativeChatGptOnV1 must be a boolean")
        if "keepNativeChatGptOnV1" in body:
            patch["keepNativeChatGptOnV1"] = keep
        client = dict(setting(config, "codexAgentSettings", {}))
        if "multiAgentMode" in body and mode in {"v1", "v2"}:
            client["enabled"] = mode == "v2" and not keep
        elif "keepNativeChatGptOnV1" in body and mode == "v2" and keep:
            client["enabled"] = False
        for key in ("enabled", "agentsEnabled"):
            if key in body:
                if not isinstance(body[key], bool) and not (
                    key == "agentsEnabled" and body[key] is None
                ):
                    raise HTTPException(400, f"{key} must be a boolean")
                client[key] = body[key]
        if (
            "enabled" in body
            and mode in {"v1", "v2"}
            and body["enabled"] != (mode == "v2" and not keep)
        ):
            raise HTTPException(400, "enabled conflicts with the selected multi-agent mode")
        for key in ("agentsMaxDepth", "maxConcurrentThreadsPerSession"):
            if key in body:
                value = body[key]
                if value is not None and (
                    type(value) is not int
                    or not -2147483648 <= value <= 2147483647
                    or key == "maxConcurrentThreadsPerSession"
                    and value < 1
                ):
                    raise HTTPException(400, f"Invalid {key}")
                client[key] = value
        for key in ("subagentDeveloperInstructions", "multiAgentModeHintText"):
            if key in body:
                value = body[key]
                if value is not None and (
                    not isinstance(value, str)
                    or key == "multiAgentModeHintText"
                    and not value.strip()
                ):
                    raise HTTPException(400, f"Invalid {key}")
                client[key] = value
        if (
            body.get("multiAgentModeHintText") is not None
            and await asyncio.to_thread(supports_mode_hint) is False
        ):
            raise HTTPException(
                409,
                "Installed Codex does not support multi_agent_mode_hint_text; update Codex first.",
            )
        patch["codexAgentSettings"] = client
        if body.get("multiAgentSurfaceAdvisoryAcknowledged") is True:
            patch["multiAgentSurfaceAdvisoryAcknowledged"] = True
        update(patch)
        await service.converge()
        return {"ok": True, **mode_state()}

    @router.get("/subagent-model-fallback")
    async def fallback():
        config = store.load()
        return {
            "models": setting(config, "subagentModelFallback", []),
            "pollMs": setting(config, "subagentModelFallbackPollMs", 60000),
            "available": [
                e["slug"] for e in await service.catalog() if e.get("visibility") == "list"
            ],
        }

    @router.put("/subagent-model-fallback")
    async def save_fallback(body: dict):
        patch = {}
        if "models" in body:
            models = body["models"]
            if not isinstance(models, list) or any(
                not isinstance(v, str) or not v.strip() for v in models
            ):
                raise HTTPException(400, "models must be an array of non-empty strings")
            patch["subagentModelFallback"] = [v.strip() for v in models] or None
        if "pollMs" in body:
            value = body["pollMs"]
            if value not in (None, "") and (type(value) is not int or not 5000 <= value <= 600000):
                raise HTTPException(400, "pollMs must be between 5000 and 600000")
            patch["subagentModelFallbackPollMs"] = value or None
        update(patch)
        return {"ok": True, **await fallback()}

    return router
