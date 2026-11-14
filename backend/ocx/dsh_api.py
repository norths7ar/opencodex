"""Original file-integration GUI contracts for DSH."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import clients
from .dsh_client import DshError, DshIntegration


def dsh_router(directory, port, model_rows, *, home=None):
    router = APIRouter(prefix="/api/client-integrations")
    integration = DshIntegration(directory, port, model_rows, home)
    router.integration = integration

    def require_dsh(body):
        if body.get("clientId") != "dsh":
            raise HTTPException(400, "This client integration is not migrated.")

    def failure(exc):
        reason = getattr(exc, "reason", "write_failed")
        return JSONResponse(
            {
                "ok": False,
                "clientId": "dsh",
                "code": "integration_mutation_failed",
                "reason": reason,
                "message": str(exc),
            },
            409,
        )

    @router.get("")
    async def statuses():
        return {"clients": [integration.status(await integration.block())]}

    @router.get("/dsh")
    async def status():
        return integration.status(await integration.block())

    @router.get("/journal")
    def journal(client: str | None = None):
        return {"operations": integration.journal() if client in {None, "dsh"} else []}

    @router.post("/preview")
    async def preview(body: dict):
        require_dsh(body)
        operation = body.get("operation")
        if operation not in {"apply", "overwrite", "disable"}:
            raise HTTPException(400, "Unknown integration operation.")
        return integration.preview(operation, await integration.block())

    @router.post("/restore/preview")
    async def restore_preview(body: dict):
        return integration.preview(
            "restore", await integration.block(), body.get("opId"), body.get("confirmDrift") is True
        )

    async def change(operation, body):
        if body.get("operation") != operation or not isinstance(body.get("planFingerprint"), str):
            raise HTTPException(400, "Preview this operation before applying it.")
        try:
            result = integration.mutate(
                operation,
                await integration.block(),
                body["planFingerprint"],
                body.get("opId"),
                body.get("confirmDrift") is True,
            )
            return JSONResponse(result, 200 if result["ok"] else 409)
        except (DshError, clients.ClientConfigError, OSError) as exc:
            return failure(exc)

    @router.put("/dsh")
    async def toggle(body: dict):
        if not isinstance(body.get("enabled"), bool):
            raise HTTPException(400, "enabled must be a boolean.")
        operation = (
            "disable"
            if not body["enabled"]
            else "overwrite"
            if body.get("overwriteConflict") is True
            else "apply"
        )
        return await change(operation, body)

    @router.post("/restore")
    async def restore(body: dict):
        return await change("restore", body)

    @router.delete("/journal")
    def delete_snapshot(opId: str):
        try:
            return integration.delete_snapshot(opId)
        except (DshError, clients.ClientConfigError, OSError) as exc:
            return failure(exc)

    return router
