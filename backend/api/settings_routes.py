"""Management routes for the retained global and request policy settings."""

import asyncio
import math
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from backend.configuration.store import RevisionConflict
from backend.integrations import clients
from backend.policies.effort import DECLARED_EFFORTS, EFFORTS
from backend.policies.search import backend_status
from backend.policies.vision import settings as vision_settings
from backend.policies.workflow import EVENT_CAPACITY

from .settings import SettingsService


def settings_router(
    service: SettingsService,
    catalog,
    integration,
    workflow,
    fallback,
    *,
    auth_status=None,
    pacer=None,
    sizing=None,
) -> APIRouter:
    """Dependencies are explicit; catalog is a synchronous effective-model snapshot callback."""
    router = APIRouter(prefix="/api")

    def config():
        return service.store.read().configuration

    def visible():
        return [m.routed_selector for m in catalog() if m.visible]

    async def converge():
        try:
            return await integration.sync()
        except (ValueError, RuntimeError, OSError) as exc:
            return {"status": "pending", "message": str(exc)}

    def checked(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def request_state():
        r = config().features.request
        return {
            "compactionRouting": r.compaction_routing,
            "codexClientCompaction": r.codex_client_compaction,
        }

    @router.get("/settings")
    def get_settings():
        return request_state()

    @router.put("/settings")
    async def put_settings(body: dict):
        names = {
            "compactionRouting": "compaction_routing",
            "codexClientCompaction": "codex_client_compaction",
        }
        if not body or body.keys() - names.keys():
            raise HTTPException(400, "Provide a migrated gateway setting.")
        checked(service.update_request, {names[k]: v for k, v in body.items()})
        result = request_state()
        if "codexClientCompaction" in body:
            result["catalogRefresh"] = await converge()
        return result

    @router.get("/simple-fallback")
    def get_fallback():
        return fallback.state()

    @router.put("/simple-fallback")
    def put_fallback(body: dict):
        checked(service.update_request, {"simple_fallback": body})
        return fallback.state()

    @router.get("/workflow-budget/settings")
    def workflow_settings():
        return workflow.settings()

    @router.put("/workflow-budget/settings")
    def set_workflow(body: Annotated[Any, Body()] = None):
        checked(service.update_request, {"workflow_budget": body})
        return workflow.settings()

    @router.get("/workflow-budget")
    def workflow_view(root: str | None = None, limit: int = 20):
        if root is not None and (not root.strip() or len(root.strip()) > 200):
            raise HTTPException(400, "Invalid root.")
        if limit < 0:
            raise HTTPException(400, "Invalid limit.")
        return workflow.view(root.strip() if root else None, min(limit, EVENT_CAPACITY))

    @router.post("/workflow-budget/clear")
    def workflow_clear(body: dict):
        root = body.get("root")
        if not isinstance(root, str) or not root.strip() or len(root.strip()) > 200:
            raise HTTPException(400, "Invalid root.")
        before = workflow.clear(root.strip())
        if before is None:
            raise HTTPException(404, "Unknown root.")
        return {"cleared": True, "root": root.strip(), "before": before}

    @router.get("/spend-settings")
    def spend_settings():
        return config().features.request.spend or {}

    @router.put("/spend-settings")
    def put_spend(body: Annotated[dict | None, Body()] = None):
        checked(service.update_request, {"spend": body})
        return spend_settings()

    @router.get("/effort-caps")
    def effort_caps():
        a = config().features.agents
        return {
            "effortCap": a.effort_cap,
            "subagentEffortCap": a.subagent_effort_cap,
            "modelPinnedEfforts": a.model_pinned_efforts,
            "efforts": list(EFFORTS),
        }

    @router.put("/effort-caps")
    def put_effort(body: dict):
        if body.keys() - {"effortCap", "subagentEffortCap", "modelPinnedEfforts"}:
            raise HTTPException(400, "Unknown effort setting.")
        patch = {}
        for field, target in [
            ("effortCap", "effort_cap"),
            ("subagentEffortCap", "subagent_effort_cap"),
        ]:
            if field in body:
                if body[field] not in (*EFFORTS, "", None):
                    raise HTTPException(400, "Invalid effort cap.")
                patch[target] = body[field] or None
        if "modelPinnedEfforts" in body:
            incoming = body["modelPinnedEfforts"]
            pins = (
                dict(config().features.agents.model_pinned_efforts) if incoming is not None else {}
            )
            if incoming is not None and not isinstance(incoming, dict):
                raise HTTPException(400, "Invalid model pins.")
            for model, effort in (incoming or {}).items():
                if (
                    not isinstance(model, str)
                    or not model.strip()
                    or effort not in (*DECLARED_EFFORTS, "", None)
                ):
                    raise HTTPException(400, "Invalid model pin.")
                if effort:
                    pins[model.strip()] = effort
                else:
                    pins.pop(model.strip(), None)
            patch["model_pinned_efforts"] = pins
        checked(service.update_agents, patch)
        return effort_caps()

    @router.get("/subagent-models")
    def roster():
        a = config().features.agents
        options = visible()
        return {
            "chosen": a.subagent_models,
            "available": list(dict.fromkeys([*options, *a.subagent_models])),
            "catalogState": {"state": "unknown", "runningCount": 0},
            "force": None,
            "forceAvailable": [],
            "forceStatus": None,
            "pickerAvailable": options,
            "pickerOrder": a.model_picker_order,
            "pickerOrderMode": a.model_picker_order_mode,
        }

    @router.put("/subagent-models")
    async def put_roster(body: dict):
        if body.keys() - {"models", "pickerOrder", "pickerOrderMode"}:
            raise HTTPException(400, "Unknown roster setting.")
        patch = {}
        if "models" in body:
            patch["subagent_models"] = body["models"]
        if "pickerOrder" in body:
            patch["model_picker_order"] = body["pickerOrder"] or []
        if "pickerOrderMode" in body:
            patch["model_picker_order_mode"] = body["pickerOrderMode"]
        checked(service.update_agents, patch, visible_models=set(visible()))
        refresh = await converge()
        value = roster()
        return {
            "ok": True,
            "applied": value["chosen"],
            "pickerOrder": value["pickerOrder"],
            "pickerOrderMode": value["pickerOrderMode"],
            "catalogRefresh": refresh,
        }

    @router.get("/subagent-model-fallback")
    def subagent_fallback():
        a = config().features.agents
        return {
            "models": a.subagent_model_fallback,
            "pollMs": a.subagent_model_fallback_poll_ms,
            "available": visible(),
        }

    @router.put("/subagent-model-fallback")
    def put_subagent_fallback(body: dict):
        if body.keys() - {"models", "pollMs"}:
            raise HTTPException(400, "Unknown fallback setting.")
        patch = {}
        if "models" in body:
            patch["subagent_model_fallback"] = body["models"]
        if "pollMs" in body:
            patch["subagent_model_fallback_poll_ms"] = body["pollMs"] or 60000
        checked(service.update_agents, patch)
        return {"ok": True, **subagent_fallback()}

    @router.get("/injection-model")
    def defaults():
        a = config().features.agents
        return {
            "syncCodexSubagentDefaults": bool(a.sync_codex_subagent_defaults and a.injection_model),
            "model": a.injection_model,
            "effort": a.injection_effort,
            "efforts": list(EFFORTS),
            "available": [
                {
                    "provider": m.identity.channel_id,
                    "model": m.identity.native_id,
                    "namespaced": m.routed_selector,
                }
                for m in catalog()
                if m.visible
            ],
        }

    @router.put("/injection-model")
    async def put_defaults(body: dict):
        names = {
            "syncCodexSubagentDefaults": "sync_codex_subagent_defaults",
            "model": "injection_model",
            "effort": "injection_effort",
        }
        if not body or body.keys() - names.keys():
            raise HTTPException(400, "Unknown default setting.")
        checked(
            service.update_agents,
            {
                names[k]: v or None if k != "syncCodexSubagentDefaults" else v
                for k, v in body.items()
            },
        )
        await converge()
        return {"ok": True, **defaults()}

    @router.get("/codex-agent-settings")
    def native_settings():
        path = integration.home() / "config.toml"
        doc = clients._parse(clients._read(path, 1 << 20, "Codex config.toml")).unwrap()
        feature = doc.get("features", {}).get("multi_agent_v2", False)
        feature = {"enabled": feature} if isinstance(feature, bool) else feature
        agents = doc.get("agents", {})
        return {
            "enabled": feature.get("enabled", bool(feature)),
            "maxConcurrentThreadsPerSession": feature.get(
                "max_concurrent_threads_per_session", agents.get("max_threads")
            ),
            "agentsEnabled": agents.get("enabled"),
            "agentsMaxDepth": agents.get("max_depth"),
            "agentsMaxThreadsConflict": False,
            "agentsMaxDepthAppliesWhenV2Disabled": not feature.get("enabled", bool(feature)),
            "pendingClientApply": not integration.record().get("enabled", False),
        }

    @router.put("/codex-agent-settings")
    async def put_native(body: dict):
        checked(service.update_agents, {"codex_agent_settings": body})
        await converge()
        return {"ok": True, **native_settings()}

    @router.put("/selected-models")
    async def select_models(body: dict):
        checked(service.select_models, body.get("provider"), body.get("models"))
        return {
            "ok": True,
            "provider": body["provider"],
            "selected": body["models"],
            "catalogRefresh": await converge(),
        }

    @router.put("/model-visibility")
    async def visibility(body: dict):
        targets = body.get("targets")
        scope = body.get("scope")
        if (
            scope not in {"models", "provider"}
            or not isinstance(targets, list)
            or not targets
            or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) for x in targets)
        ):
            raise HTTPException(400, "Invalid visibility operation.")
        result = checked(
            service.set_visibility,
            body.get("provider"),
            [x["id"] for x in targets],
            body.get("enabled"),
            all_channel=scope == "provider",
        )
        channel = next(c for c in config().channels if c.id == body["provider"])
        return {
            "ok": True,
            "disabled": result["models"]["disabled"],
            "selected": channel.models.selected,
            "clientIntegrations": [],
            "catalogRefresh": await converge(),
        }

    @router.put("/disabled-models")
    async def disabled_models(body: dict):
        result = checked(service.set_disabled_models, body.get("models", []))
        return {
            "ok": True,
            "disabled": result["models"]["disabled"],
            "catalogRefresh": await converge(),
        }

    @router.get("/provider-context-caps")
    def caps():
        m = config().models
        active = m.context_caps or {}
        return {
            "cap": 350000,
            "value": m.context_cap_value or 350000,
            "caps": active,
            "values": {**(m.context_cap_values or {}), **active},
        }

    @router.put("/provider-context-caps")
    async def set_caps(body: dict):
        checked(
            service.set_context_caps,
            channel_id=body.get("provider"),
            enabled=body.get("enabled"),
            value=body.get("value"),
            set_all=body.get("setAll"),
        )
        return {"ok": True, **caps(), "catalogRefresh": await converge()}

    @router.get("/model-discovery")
    def discovery():
        cfg = config()
        state = cfg.models.discovery or {}
        disabled = set(cfg.models.disabled)
        return {
            "policy": state.get("newModelPolicy", "on"),
            "providers": {
                c.id: c.models.metadata.new_model_policy or "inherit" for c in cfg.channels
            },
            "recentArrivals": {
                name: [
                    {
                        **row,
                        "state": "auto-disabled"
                        if f"{name}/{row['id'].replace('/', '-')}" in disabled
                        else "enabled",
                    }
                    for row in rows
                ]
                for name, rows in state.get("recentArrivals", {}).items()
            },
            "baselineCounts": {
                name: len(row["ids"]) for name, row in state.get("knownModels", {}).items()
            },
        }

    @router.put("/model-discovery")
    async def set_discovery(body: dict):
        checked(service.set_discovery_policy, body.get("policy"), channel_id=body.get("provider"))
        return {
            "ok": True,
            "policy": body.get("policy"),
            "provider": body.get("provider"),
            "catalogRefresh": await converge(),
        }

    @router.post("/model-discovery/acknowledge")
    async def acknowledge(body: dict):
        checked(service.acknowledge_arrivals, body.get("provider"), body.get("ids"))
        return {
            "ok": True,
            "provider": body["provider"],
            "acknowledged": list(dict.fromkeys(body["ids"])),
            "catalogRefresh": await converge(),
        }

    @router.get("/model-presets")
    def model_presets():
        # No retained Lite service has one of the original curated roster presets.
        return {"providers": {}}

    @router.put("/model-presets")
    async def set_model_preset(body: dict):
        mode, name = body.get("mode"), body.get("provider")
        checked(service.set_model_preset, name, mode)
        channel = next(item for item in config().channels if item.id == name)
        return {
            "ok": True,
            "provider": name,
            "mode": mode,
            "selected": channel.models.selected,
            "catalogRefresh": await converge(),
        }

    @router.get("/provider-request-pacing")
    def pacing_status(name: str | None = None):
        channels = {item.id: item for item in config().channels}
        if name is not None and name not in channels:
            raise HTTPException(404, "unknown provider")

        def status(channel):
            policy = channel.request_pacing
            state = pacer.states.get(channel.id) if pacer is not None else None
            now = pacer.clock() if pacer is not None else 0
            return {
                "provider": channel.id,
                "enabled": bool(policy and policy.enabled),
                "queued": len(state.queue) if state else 0,
                "inFlight": state.active if state and policy and policy.enabled else 0,
                "nextSlotInMs": max(0, math.ceil((state.next_start - now) * 1000)) if state else 0,
            }

        return (
            status(channels[name])
            if name is not None
            else {key: status(value) for key, value in channels.items()}
        )

    @router.post("/injection-model/suggest")
    async def suggest_defaults(body: dict):
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
        if sizing is None:
            raise HTTPException(503, "The gateway sizing transport is unavailable.")
        try:
            async with asyncio.timeout(180):
                return await sizing(work.strip(), model.strip())
        except TimeoutError:
            raise HTTPException(
                504, "The model recommendation timed out; retry when available."
            ) from None

    def sidecars():
        cfg = config()
        search = cfg.features.request.web_search_sidecar or {}
        available = (
            {"openai"}
            if any(
                c.service_id == "chatgpt"
                and not c.disabled
                and auth_status
                and auth_status(c.id).get("loggedIn")
                for c in cfg.channels
            )
            else set()
        )
        rows = list(catalog())
        channels = {channel.id: channel for channel in cfg.channels}
        options = []
        search_models = {}
        for model in rows:
            channel = channels.get(model.identity.channel_id)
            if channel is None or model.disabled:
                continue
            modes = model.metadata.values.get("inputModalities")
            if model.selected and (modes is None or "image" in modes):
                options.append(
                    {
                        "value": model.routed_selector,
                        "label": model.routed_selector,
                        "backend": "routed",
                    }
                )
                if channel.service_id == "chatgpt" and channel.auth_mode == "oauth":
                    options.append(
                        {
                            "value": model.identity.native_id,
                            "label": model.routed_selector,
                            "backend": "openai",
                        }
                    )
            if (
                channel.service_id == "chatgpt"
                and channel.auth_mode == "oauth"
                and "openai" in available
                and "/" not in model.identity.native_id
            ):
                identity = model.identity.native_id
                search_models[identity] = {
                    "value": identity,
                    "label": identity,
                    "backend": "openai",
                    "model": identity,
                }
        saved = search.get("model")
        if "openai" in available:
            search_models.setdefault(
                "gpt-5.6-luna",
                {
                    "value": "gpt-5.6-luna",
                    "label": "gpt-5.6-luna",
                    "backend": "openai",
                    "model": "gpt-5.6-luna",
                    "authSlot": True,
                },
            )
        if saved and saved not in search_models:
            search_models[saved] = {
                "value": saved,
                "label": saved,
                "backend": search.get("backend", "openai"),
                "model": saved,
            }
        return {
            "vision": vision_settings(cfg, auth_status),
            "visionModels": options,
            "webSearch": {
                "enabled": search.get("enabled") is not False,
                "model": search.get("model", "gpt-5.6-luna"),
                "backend": search.get("backend"),
                "streamRoutedModelOutput": search.get("streamRoutedModelOutput") is True,
            },
            "webSearchModels": sorted(
                search_models.values(), key=lambda row: row["value"].casefold()
            ),
            "webSearchStatus": backend_status(search, available),
        }

    @router.get("/sidecar-settings")
    def get_sidecars():
        return sidecars()

    @router.put("/sidecar-settings")
    async def put_sidecars(body: dict):
        if (
            not body
            or body.keys() - {"vision", "webSearch"}
            or any(not isinstance(v, dict) for v in body.values())
        ):
            raise HTTPException(400, "Provide vision or webSearch settings objects.")
        patch = {}
        snapshot = service.store.read()
        current = snapshot.configuration.features.request
        for name, key, prior, allowed in [
            (
                "vision",
                "vision_sidecar",
                current.vision_sidecar,
                {"enabled", "backend", "model", "reasoning", "maxDescriptionsPerTurn", "timeoutMs"},
            ),
            (
                "webSearch",
                "web_search_sidecar",
                current.web_search_sidecar,
                {"enabled", "backend", "model", "reasoning", "streamRoutedModelOutput"},
            ),
        ]:
            if name not in body:
                continue
            incoming = body[name]
            if incoming.keys() - allowed:
                raise HTTPException(400, "Unknown sidecar setting.")
            if "enabled" in incoming and type(incoming["enabled"]) is not bool:
                raise HTTPException(400, "enabled must be boolean.")
            if (
                "streamRoutedModelOutput" in incoming
                and type(incoming["streamRoutedModelOutput"]) is not bool
            ):
                raise HTTPException(400, "streamRoutedModelOutput must be boolean.")
            if "reasoning" in incoming and incoming["reasoning"] not in EFFORTS:
                raise HTTPException(400, "Invalid reasoning effort.")
            if "model" in incoming and (
                not isinstance(incoming["model"], str) or not incoming["model"].strip()
            ):
                raise HTTPException(400, "Invalid model.")
            for field, low, high in [
                ("maxDescriptionsPerTurn", 0, 2**53 - 1),
                ("timeoutMs", 1, 2147483647),
            ]:
                if field in incoming and (
                    type(incoming[field]) is not int or not low <= incoming[field] <= high
                ):
                    raise HTTPException(400, f"Invalid {field}.")
            merged = {**(prior or {}), **incoming}
            backend = merged.get("backend") or "openai"
            if name == "vision":
                if backend not in {"openai", "routed"}:
                    raise HTTPException(400, "Select an OpenAI or routed helper.")
                if "model" in incoming or "backend" in incoming:
                    model = merged.get("model") or ("gpt-5.6-luna" if backend == "openai" else "")
                    if (backend == "routed") != ("/" in model):
                        raise HTTPException(400, "Routed helpers require a channel/model selector.")
                    matches = [
                        row
                        for row in catalog()
                        if (
                            row.routed_selector == model
                            if backend == "routed"
                            else row.identity.native_id == model
                            and any(
                                c.id == row.identity.channel_id and c.service_id == "chatgpt"
                                for c in snapshot.configuration.channels
                            )
                        )
                    ]
                    if not matches or all(
                        row.disabled
                        or (
                            row.metadata.values.get("inputModalities") is not None
                            and "image" not in row.metadata.values["inputModalities"]
                        )
                        for row in matches
                    ):
                        raise HTTPException(400, "Select a configured image-capable helper model.")
            else:
                if backend != "openai":
                    raise HTTPException(400, "Selected search backend is not supported.")
                if "model" in incoming:
                    identity = merged["model"]
                    candidates = {
                        row.identity.native_id
                        for row in catalog()
                        if not row.disabled
                        and any(
                            c.id == row.identity.channel_id and c.service_id == "chatgpt"
                            for c in snapshot.configuration.channels
                        )
                    } | {"gpt-5.6-luna"}
                    if identity not in candidates:
                        raise HTTPException(400, "Select a model supported by the search backend.")
            patch[key] = {**(prior or {}), **incoming}
        try:
            service.update_request(patch, expected_revision=snapshot.revision)
        except RevisionConflict:
            raise HTTPException(409, "Sidecar validation state changed; retry.") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        applied = await converge()
        return {
            "ok": True,
            **sidecars(),
            "catalogRefresh": applied,
            "catalogRefreshPending": applied.get("status") == "pending",
            "codexWebSearch": {"applied": applied.get("status") == "applied"},
        }

    return router
