"""Web console actions for the project's Windows background startup."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException

from .startup import Startup


def startup_router(directory: Path, port: int, *, client_status=None):
    router = APIRouter(prefix="/api")
    startup = Startup(directory, port)

    @router.get("/startup-health")
    def health():
        result = startup.health()
        injected = bool(client_status and client_status().get("state") == "current")
        return {
            **result,
            "routingInjected": injected,
            "routingKind": "opencodex-local" if injected else "native",
            "localRoutingDependency": injected,
        }

    @router.post("/startup-action")
    def action(body: Annotated[dict, Body()]):
        selected = body.get("action")
        repair = body.get("repair", False)
        if not isinstance(selected, str) or not isinstance(repair, bool):
            raise HTTPException(
                400, "Provide a startup action and an optional boolean repair flag."
            )
        return startup.action(selected, repair=repair)

    return router
