"""Management adapters for request telemetry, local diagnostics, and storage."""

import math
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.configuration.store import ConfigurationStore
from backend.telemetry.debug import DebugState
from backend.telemetry.ledger import UsageStore

from .observability_storage import Storage
from .observability_usage import usage_router


def observability_router(
    directory: Path,
    usage: UsageStore,
    config: ConfigurationStore,
    debug: DebugState,
    *,
    official_prices: Callable[[], dict] | None = None,
) -> APIRouter:
    router = APIRouter()
    router.include_router(usage_router(usage, config, official_prices))
    router.include_router(debug_router(debug))
    router.include_router(storage_router(directory, usage))
    return router


def debug_router(state):
    router = APIRouter(prefix="/api/debug")

    @router.get("")
    def settings():
        return state.settings()

    @router.put("")
    async def configure(request: Request):
        try:
            return state.configure(await request.json())
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def query(request, stream):
        def number(value, default):
            try:
                result = float(value)
                return result if math.isfinite(result) and result > 0 else default
            except ValueError, TypeError:
                return default

        after = number(request.query_params.get("after", request.query_params.get("since")), 0)
        limit = min(2000, int(number(request.query_params.get("limit"), 500)))
        return state.entries(stream, after=after, limit=max(1, limit))

    @router.get("/logs")
    def logs(request: Request):
        return query(request, "provider")

    @router.get("/usage-logs")
    def usage(request: Request):
        return query(request, "usage")

    @router.get("/injection-logs")
    def injection(request: Request):
        return query(request, "injection")

    return router


class CleanupRequest(BaseModel):
    digest: str


def storage_router(directory, usage=None):
    router = APIRouter(prefix="/api/lite-storage")
    service = Storage(directory, usage)

    @router.get("")
    def report():
        return {**service.inventory(), "quarantine": service.list_quarantine()}

    @router.get("/preview")
    def preview():
        return service.preview()

    @router.post("/quarantine")
    def quarantine(body: CleanupRequest):
        try:
            return service.quarantine(body.digest)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/restore/{identity}")
    def restore(identity: str):
        try:
            return service.restore(identity)
        except (ValueError, TypeError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
