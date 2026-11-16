"""HTTP adapters for the selected Codex and DSH client integrations."""

from typing import Annotated

import anyio
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from backend.catalog.identity import ModelIdentity
from backend.integrations import client_catalog, clients
from backend.integrations.codex import CodexIntegration
from backend.integrations.dsh import DshError, DshIntegration

from . import integration_process


def integration_router(
    integration: CodexIntegration, dsh: DshIntegration, *, process=integration_process
) -> APIRouter:
    """Mount explicit client operations on services configured by the application."""
    router = APIRouter(prefix="/api")
    store, directory = integration.store, integration.directory

    @router.get("/clients/codex")
    def preview(home: str | None = None):
        target = integration.home(home)
        config = store.read().configuration
        channel = next((p for p in config.channels if p.id == config.default_channel_id), None)
        model = (channel.models.default if channel else None) or config.default_model_id
        selector = ModelIdentity(channel.id, model).routed_selector if channel and model else ""
        return {
            **clients.preview(target, integration.base_url, selector, directory),
            "catalogPath": str(client_catalog.catalog_path(target, directory)),
            "home": str(target),
            "integration": integration.status(home),
        }

    @router.post("/clients/codex/configure")
    async def configure(body: Annotated[dict | None, Body()] = None):
        home = (body or {}).get("home")
        outcome = await integration.sync(home, enable=True)
        return {**clients.status(integration.home(home), directory), **outcome}

    @router.post("/clients/codex/restore")
    def restore(body: Annotated[dict | None, Body()] = None):
        return integration.restore((body or {}).get("home"))

    @router.get("/native-integrations")
    def native_status():
        return {"clients": [integration.status()]}

    @router.put("/native-integrations/codex")
    async def toggle(body: dict):
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled must be a boolean")
        before = integration.status(body.get("home"))
        try:
            if enabled:
                outcome = await integration.sync(body.get("home"), enable=True)
            else:
                integration.restore(body.get("home"))
                outcome = {"message": "Codex configuration restored; the gateway remains running."}
        except (ValueError, RuntimeError, OSError) as exc:
            return JSONResponse(
                {
                    "error": "Codex integration change failed",
                    "code": "native_integration_failed",
                    "clientId": "codex",
                    "reason": "write_failed",
                    "message": str(exc),
                    "desiredEnabled": enabled,
                },
                status_code=409,
            )
        after = integration.status(body.get("home"))
        return {
            "ok": True,
            "clientId": "codex",
            "changed": before != after,
            "state": after["state"],
            "desiredEnabled": enabled,
            "message": outcome["message"],
        }

    @router.post("/sync")
    async def sync(body: Annotated[dict | None, Body()] = None):
        return await integration.sync((body or {}).get("home"))

    @router.get("/system/codex-app-server")
    def process_status():
        return process.freshness(client_catalog.catalog_path(integration.home(), directory))

    @router.post("/system/codex-restart")
    async def restart():
        synced = await integration.sync()
        return await anyio.to_thread.run_sync(
            lambda: process.restart(
                client_catalog.catalog_path(integration.home(), directory),
                synced=bool(synced["catalogWritten"] or synced["cacheSynced"]),
            )
        )

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

    @router.get("/client-integrations")
    async def statuses():
        return {"clients": [dsh.status(await dsh.block())]}

    @router.get("/client-integrations/dsh")
    async def status():
        return dsh.status(await dsh.block())

    @router.get("/client-integrations/journal")
    def journal(client: str | None = None):
        return {"operations": dsh.journal() if client in {None, "dsh"} else []}

    @router.post("/client-integrations/preview")
    async def preview_dsh(body: dict):
        require_dsh(body)
        operation = body.get("operation")
        if operation not in {"apply", "overwrite", "disable"}:
            raise HTTPException(400, "Unknown integration operation.")
        return dsh.preview(operation, await dsh.block())

    @router.post("/client-integrations/restore/preview")
    async def restore_preview(body: dict):
        return dsh.preview(
            "restore", await dsh.block(), body.get("opId"), body.get("confirmDrift") is True
        )

    async def change(operation, body):
        if body.get("operation") != operation or not isinstance(body.get("planFingerprint"), str):
            raise HTTPException(400, "Preview this operation before applying it.")
        try:
            result = dsh.mutate(
                operation,
                await dsh.block(),
                body["planFingerprint"],
                body.get("opId"),
                body.get("confirmDrift") is True,
            )
            return JSONResponse(result, 200 if result["ok"] else 409)
        except (DshError, clients.ClientConfigError, OSError) as exc:
            return failure(exc)

    @router.put("/client-integrations/dsh")
    async def toggle_dsh(body: dict):
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

    @router.post("/client-integrations/restore")
    async def restore_dsh(body: dict):
        return await change("restore", body)

    @router.delete("/client-integrations/journal")
    def delete_snapshot(opId: str):
        try:
            return dsh.delete_snapshot(opId)
        except (DshError, clients.ClientConfigError, OSError) as exc:
            return failure(exc)

    return router
