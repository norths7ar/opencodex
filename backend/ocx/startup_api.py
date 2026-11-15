"""Web console actions for the project's Windows background startup."""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException
from fastapi.responses import JSONResponse

from .service_cli import launch_service_command, record_operation, service_command
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
    def action(body: Annotated[dict, Body()], background_tasks: BackgroundTasks):
        selected = body.get("action")
        repair = body.get("repair", False)
        if not isinstance(selected, str) or not isinstance(repair, bool):
            raise HTTPException(
                400, "Provide a startup action and an optional boolean repair flag."
            )
        if selected == "install-service":
            selected = "repair" if repair else "install"
        if selected not in {"install", "repair", "enable", "disable", "remove"}:
            raise HTTPException(400, "Unknown service action.")
        registration = startup.registration()
        if registration["conflict"]:
            raise HTTPException(409, "A different task occupies the Lite service name.")
        needs_restart = (
            selected == "repair"
            and registration["enabled"]
            and (registration["stale"] or startup.environment_changed())
        )
        if selected in {"install", "remove"} or needs_restart:
            identity = uuid4().hex
            record_operation(directory, identity, selected, "running")
            background_tasks.add_task(launch_service_command, directory, port, selected, identity)
            return JSONResponse(
                {"ok": True, "pending": True, "action": selected, "operationId": identity},
                status_code=202,
            )
        return service_command(directory, port, selected)

    return router
