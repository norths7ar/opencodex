"""Application operations for retained settings; HTTP only maps the commands."""

import math
from typing import Any

from backend.policies.workflow import EVENT_CAPACITY

from .errors import ApplicationError
from .settings_context import SettingsContext


class RequestPolicyManagement(SettingsContext):
    def __init__(self, *, service, catalog, workflow, fallback, pacer=None, apply_changes=None):
        super().__init__(service=service, catalog=catalog, apply_changes=apply_changes)
        self.workflow, self.fallback, self.pacer = workflow, fallback, pacer

    def request_state(self):
        r = self.config().features.request
        return {
            "compactionRouting": r.compaction_routing,
            "codexClientCompaction": r.codex_client_compaction,
        }

    def get_settings(self):
        return self.request_state()

    async def put_settings(self, body: dict):
        names = {
            "compactionRouting": "compaction_routing",
            "codexClientCompaction": "codex_client_compaction",
        }
        if not body or body.keys() - names.keys():
            raise ApplicationError(400, "Provide a migrated gateway setting.")
        self.checked(self.service.update_request, {names[k]: v for k, v in body.items()})
        result = self.request_state()
        if "codexClientCompaction" in body:
            result["catalogRefresh"] = await self.converge()
        return result

    def get_fallback(self):
        return self.fallback.state()

    def put_fallback(self, body: dict):
        self.checked(self.service.update_request, {"simple_fallback": body})
        return self.fallback.state()

    def workflow_settings(self):
        return self.workflow.settings()

    def set_workflow(self, body: Any = None):
        self.checked(self.service.update_request, {"workflow_budget": body})
        return self.workflow.settings()

    def workflow_view(self, root: str | None = None, limit: int = 20):
        if root is not None and (not root.strip() or len(root.strip()) > 200):
            raise ApplicationError(400, "Invalid root.")
        if limit < 0:
            raise ApplicationError(400, "Invalid limit.")
        return self.workflow.view(root.strip() if root else None, min(limit, EVENT_CAPACITY))

    def workflow_clear(self, body: dict):
        root = body.get("root")
        if not isinstance(root, str) or not root.strip() or len(root.strip()) > 200:
            raise ApplicationError(400, "Invalid root.")
        before = self.workflow.clear(root.strip())
        if before is None:
            raise ApplicationError(404, "Unknown root.")
        return {"cleared": True, "root": root.strip(), "before": before}

    def spend_settings(self):
        return self.config().features.request.spend or {}

    def put_spend(self, body: dict | None = None):
        self.checked(self.service.update_request, {"spend": body})
        return self.spend_settings()

    def pacing_status(self, name: str | None = None):
        channels = {item.id: item for item in self.config().channels}
        if name is not None and name not in channels:
            raise ApplicationError(404, "unknown provider")

        def status(channel):
            policy = channel.request_pacing
            state = self.pacer.states.get(channel.id) if self.pacer is not None else None
            now = self.pacer.clock() if self.pacer is not None else 0
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
