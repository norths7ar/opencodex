"""HTTP query adaptation for the read-only observability application service."""

from fastapi import APIRouter, Request, Response

from backend.application.observability_queries import ObservabilityQueries


def usage_router(store, config_store, official_prices=None):
    router = APIRouter(prefix="/api")
    queries = ObservabilityQueries(store, config_store, official_prices=official_prices)

    @router.get("/logs")
    def logs(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return queries.logs(request.query_params)

    @router.get("/usage/source-facets")
    def facets(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return queries.facets(request.query_params)

    @router.get("/usage")
    def usage(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return queries.usage(request.query_params)

    @router.get("/usage/timeline")
    def timeline(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return queries.timeline(request.query_params)

    return router
