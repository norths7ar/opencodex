"""Stable homepage data: runtime state and persisted setup milestones."""

from fastapi import APIRouter

from backend.application.runtime_contracts import Overview


def overview_router(*, report, milestones, configuration, startup):
    router = APIRouter(prefix="/api")

    @router.get("/overview", response_model=Overview)
    def overview(force: bool = False):
        channels = configuration().channels
        return {
            "runtime": report(),
            "startup": startup(force=force),
            "channels": {"total": len(channels), "enabled": sum(not c.disabled for c in channels)},
            "milestones": {"first_success_at": milestones.first_success_at()},
        }

    return router
