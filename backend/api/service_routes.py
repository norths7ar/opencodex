"""Windows service management routes with detached destructive actions."""

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from backend.runtime.scheduler import Startup
from backend.runtime.service import launch_service_command, record_operation, service_command


def service_router(directory: Path, port: int, *, client_status=None, startup=None):
    router = APIRouter(prefix="/api")
    manager = startup or Startup(directory, port)

    @router.get("/startup-health")
    def health():
        result = manager.health()
        injected = bool(client_status and client_status().get("state") == "current")
        return {
            **result,
            "routingInjected": injected,
            "routingKind": "opencodex-local" if injected else "native",
            "localRoutingDependency": injected,
        }

    @router.post("/startup-action")
    def action(body: dict, background_tasks: BackgroundTasks):
        selected, repair = body.get("action"), body.get("repair", False)
        if not isinstance(selected, str) or type(repair) is not bool:
            raise HTTPException(
                400, "Provide a service action and an optional boolean repair flag."
            )
        if selected == "install-service":
            selected = "repair" if repair else "install"
        if selected not in {
            "install",
            "repair",
            "enable",
            "disable",
            "remove",
            "start",
            "stop",
            "restart",
        }:
            raise HTTPException(400, "Unknown service action.")
        registration = manager.registration()
        if registration["conflict"]:
            raise HTTPException(409, "A different task occupies the Lite service name.")
        needs_restart = (
            selected == "repair"
            and registration["enabled"]
            and (registration["stale"] or manager.environment_changed())
        )
        if selected in {"install", "remove", "stop", "restart"} or needs_restart:
            identity = uuid4().hex
            record_operation(directory, identity, selected, "running")
            background_tasks.add_task(launch_service_command, directory, port, selected, identity)
            return JSONResponse(
                {"ok": True, "pending": True, "action": selected, "operationId": identity},
                status_code=202,
            )
        return service_command(directory, port, selected)

    return router
