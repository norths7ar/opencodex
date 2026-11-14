"""Original workflow count settings, observations and targeted-clear contracts."""

import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from .workflow_budget import EVENT_CAPACITY, validate_settings


def workflow_router(store, budget):
    router = APIRouter(prefix="/api/workflow-budget")

    def fail(code, message, status=400):
        raise HTTPException(status, {"error": {"code": code, "message": message}})

    def root_id(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            fail("invalid_root", "root must be a non-empty string of at most 200 characters")
        return value.strip()

    @router.get("/settings")
    def settings():
        return budget.settings()

    @router.put("/settings")
    async def save_settings(request: Request):
        try:
            body = await request.json()
        except ValueError:
            fail("invalid_workflow_budget", "Provide JSON null or an object of count ceilings.")
        if body is not None:
            try:
                validate_settings(body)
            except ValueError as exc:
                fail("invalid_workflow_budget", str(exc))
        with store.mutation_lock:
            config = store.load()
            if body is None:
                config.__pydantic_extra__.pop("workflowBudget", None)
            else:
                old = getattr(config, "workflowBudget", {})
                try:
                    validate_settings(old)
                except ValueError:
                    old = {}
                config.workflowBudget = {**old, **body}
            store.save(config)
        return budget.settings()

    @router.get("")
    def view(root: str | None = None, limit: str | None = None):
        if limit is not None and not re.fullmatch(r"[0-9]+", limit):
            fail("invalid_limit", "limit must be a non-negative integer")
        wanted = min(EVENT_CAPACITY, int(limit.lstrip("0")[:3] or "0")) if limit is not None else 20
        return budget.view(root_id(root) if root is not None else None, wanted)

    @router.post("/clear")
    def clear(body: Annotated[Any, Body()] = None):
        root = root_id(body.get("root") if isinstance(body, dict) else None)
        before = budget.clear(root)
        if before is None:
            fail("unknown_root", "root is not currently tracked", 404)
        return {"cleared": True, "root": root, "before": before}

    return router
