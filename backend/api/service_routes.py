"""Windows runtime locations and service management."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.application.errors import ApplicationError
from backend.platform import explorer
from backend.runtime.scheduler import Startup
from backend.runtime.service import (
    launch_service_command,
    plan_operation,
    record_operation,
    service_command,
)

_LOCATION_FILES = {"state": None, "runtime-log": "runtime.log", "startup-log": "startup.log"}


class RuntimeLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: Literal["state", "runtime-log", "startup-log"]


def service_router(directory: Path, port: int, *, client_status=None, startup=None):
    router = APIRouter(prefix="/api")
    manager = startup or Startup(directory, port)

    @router.post("/runtime/open-location")
    def open_location(body: RuntimeLocation):
        try:
            explorer.open_location(directory, filename=_LOCATION_FILES[body.location])
        except NotImplementedError as exc:
            raise ApplicationError(
                501,
                "Opening a location requires Windows File Explorer.",
                code="unsupported_platform",
            ) from exc
        except OSError as exc:
            raise ApplicationError(
                503, "Could not open the location in Windows File Explorer."
            ) from exc
        return {"ok": True}

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
        operation = plan_operation(directory, port, selected, startup=manager)
        selected = operation["action"]
        if operation["detached"]:
            identity = uuid4().hex
            record_operation(directory, identity, selected, "running")
            background_tasks.add_task(launch_service_command, directory, port, selected, identity)
            return JSONResponse(
                {"ok": True, "pending": True, "action": selected, "operationId": identity},
                status_code=202,
            )
        return service_command(directory, port, selected)

    return router
