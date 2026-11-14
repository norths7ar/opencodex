"""Original Debug page settings and cursor-tail routes."""

import math

from fastapi import APIRouter, HTTPException, Request


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
