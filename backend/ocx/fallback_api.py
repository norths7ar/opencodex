"""Management contract for Lite's explicit ordered backup models."""

from fastapi import APIRouter

from .fallback import FallbackSettings, SimpleFallback


def fallback_router(fallback: SimpleFallback):
    router = APIRouter(prefix="/api")

    @router.get("/simple-fallback")
    def state():
        return fallback.state()

    @router.put("/simple-fallback")
    def save(body: FallbackSettings):
        return fallback.save(body)

    return router
