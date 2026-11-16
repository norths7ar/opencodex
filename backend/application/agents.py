"""Application operations for retained settings; HTTP only maps the commands."""

import asyncio

from backend.policies.effort import DECLARED_EFFORTS, EFFORTS

from .errors import ApplicationError
from .settings_context import SettingsContext


class AgentManagement(SettingsContext):
    def __init__(self, *, service, catalog, integration, sizing=None, apply_changes=None):
        super().__init__(service=service, catalog=catalog, apply_changes=apply_changes)
        self.integration, self.sizing = integration, sizing

    def effort_caps(self):
        a = self.config().features.agents
        return {
            "effortCap": a.effort_cap,
            "subagentEffortCap": a.subagent_effort_cap,
            "modelPinnedEfforts": a.model_pinned_efforts,
            "efforts": list(EFFORTS),
        }

    def put_effort(self, body: dict):
        if body.keys() - {"effortCap", "subagentEffortCap", "modelPinnedEfforts"}:
            raise ApplicationError(400, "Unknown effort setting.")
        patch = {}
        for field, target in [
            ("effortCap", "effort_cap"),
            ("subagentEffortCap", "subagent_effort_cap"),
        ]:
            if field in body:
                if body[field] not in (*EFFORTS, "", None):
                    raise ApplicationError(400, "Invalid effort cap.")
                patch[target] = body[field] or None
        if "modelPinnedEfforts" in body:
            incoming = body["modelPinnedEfforts"]
            pins = (
                dict(self.config().features.agents.model_pinned_efforts)
                if incoming is not None
                else {}
            )
            if incoming is not None and (not isinstance(incoming, dict)):
                raise ApplicationError(400, "Invalid model pins.")
            for model, effort in (incoming or {}).items():
                if (
                    not isinstance(model, str)
                    or not model.strip()
                    or effort not in (*DECLARED_EFFORTS, "", None)
                ):
                    raise ApplicationError(400, "Invalid model pin.")
                if effort:
                    pins[model.strip()] = effort
                else:
                    pins.pop(model.strip(), None)
            patch["model_pinned_efforts"] = pins
        self.checked(self.service.update_agents, patch)
        return self.effort_caps()

    def roster(self):
        a = self.config().features.agents
        options = self.visible()
        return {
            "chosen": a.subagent_models,
            "available": list(dict.fromkeys([*options, *a.subagent_models])),
            "catalogState": {"state": "unknown", "runningCount": 0},
            "pickerAvailable": options,
            "pickerOrder": a.model_picker_order,
            "pickerOrderMode": a.model_picker_order_mode,
        }

    async def put_roster(self, body: dict):
        if body.keys() - {"models", "pickerOrder", "pickerOrderMode"}:
            raise ApplicationError(400, "Unknown roster setting.")
        patch = {}
        if "models" in body:
            patch["subagent_models"] = body["models"]
        if "pickerOrder" in body:
            patch["model_picker_order"] = body["pickerOrder"] or []
        if "pickerOrderMode" in body:
            patch["model_picker_order_mode"] = body["pickerOrderMode"]
        self.checked(self.service.update_agents, patch, visible_models=set(self.visible()))
        refresh = await self.converge()
        value = self.roster()
        return {
            "ok": True,
            "applied": value["chosen"],
            "pickerOrder": value["pickerOrder"],
            "pickerOrderMode": value["pickerOrderMode"],
            "catalogRefresh": refresh,
        }

    def subagent_fallback(self):
        a = self.config().features.agents
        return {
            "models": a.subagent_model_fallback,
            "pollMs": a.subagent_model_fallback_poll_ms,
            "available": self.visible(),
        }

    def put_subagent_fallback(self, body: dict):
        if body.keys() - {"models", "pollMs"}:
            raise ApplicationError(400, "Unknown fallback setting.")
        patch = {}
        if "models" in body:
            patch["subagent_model_fallback"] = body["models"]
        if "pollMs" in body:
            patch["subagent_model_fallback_poll_ms"] = body["pollMs"] or 60000
        self.checked(self.service.update_agents, patch)
        return {"ok": True, **self.subagent_fallback()}

    def defaults(self):
        a = self.config().features.agents
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
                for m in self.catalog()
                if m.visible
            ],
        }

    async def put_defaults(self, body: dict):
        names = {
            "syncCodexSubagentDefaults": "sync_codex_subagent_defaults",
            "model": "injection_model",
            "effort": "injection_effort",
        }
        if not body or body.keys() - names.keys():
            raise ApplicationError(400, "Unknown default setting.")
        self.checked(
            self.service.update_agents,
            {
                names[k]: v or None if k != "syncCodexSubagentDefaults" else v
                for k, v in body.items()
            },
        )
        await self.converge()
        return {"ok": True, **self.defaults()}

    def native_settings(self):
        return self.integration.native_agent_settings()

    async def put_native(self, body: dict):
        self.checked(self.service.update_agents, {"codex_agent_settings": body})
        await self.converge()
        return {"ok": True, **self.native_settings()}

    async def suggest_defaults(self, body: dict):
        work, model = (body.get("work"), body.get("model"))
        if not isinstance(work, str) or not work.strip() or len(work) > 1500:
            raise ApplicationError(400, "work must be a nonblank string of at most 1500 characters")
        if model is not None and (not isinstance(model, str)):
            raise ApplicationError(400, "model must be a string")
        if not model or not model.strip():
            model = self.integration.default_model()
        if not model:
            raise ApplicationError(409, "No default model is set in Codex; select a sizing model.")
        if self.sizing is None:
            raise ApplicationError(503, "The gateway sizing transport is unavailable.")
        try:
            async with asyncio.timeout(180):
                return await self.sizing(work.strip(), model.strip())
        except TimeoutError:
            raise ApplicationError(
                504, "The model recommendation timed out; retry when available."
            ) from None
