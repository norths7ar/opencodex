"""Migrated gateway settings consumed by the original management panels."""

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from .compaction import CompactionRouting


def settings_router(store, integration=None):
    router = APIRouter(prefix="/api")

    def state():
        config = store.load()
        result = {
            "compactionRouting": getattr(config, "compactionRouting", None),
            "codexClientCompaction": getattr(config, "codexClientCompaction", False),
        }
        if integration is not None:
            try:
                actual = integration.status()
                desired = result["codexClientCompaction"]
                result["catalogRefreshPending"] = actual["clientCompaction"] != desired or (
                    desired and actual["state"] != "current"
                )
                result["clientApplication"] = {
                    "enabled": actual["desiredEnabled"],
                    "appliedClientCompaction": actual["clientCompaction"],
                    "warning": actual["warning"],
                    "message": actual["message"],
                }
            except ValueError, RuntimeError, OSError:
                result["catalogRefreshPending"] = True
                result["clientApplication"] = {
                    "message": "Review the client configuration in Route."
                }
        return result

    @router.get("/settings")
    def get_settings():
        return state()

    @router.put("/settings")
    async def save(body: dict):
        if not body or set(body) - {"compactionRouting", "codexClientCompaction"}:
            raise HTTPException(400, "Provide a migrated gateway setting.")
        patch = {}
        if "codexClientCompaction" in body:
            if type(body["codexClientCompaction"]) is not bool:
                raise HTTPException(400, "codexClientCompaction must be boolean.")
            patch["codexClientCompaction"] = body["codexClientCompaction"]
        if "compactionRouting" in body:
            value = body["compactionRouting"]
            if value is not None:
                try:
                    value = CompactionRouting.model_validate(value).model_dump(exclude_none=True)
                except ValidationError as exc:
                    raise HTTPException(400, "Invalid compaction routing settings.") from exc
            patch["compactionRouting"] = value
        with store.mutation_lock:
            config = store.load()
            for key, value in patch.items():
                if value is None:
                    config.model_extra.pop(key, None)
                else:
                    setattr(config, key, value)
            store.save(config)
        applied = None
        if "codexClientCompaction" in patch and integration is not None:
            try:
                applied = await integration.sync()
            except (ValueError, RuntimeError, OSError) as exc:
                applied = {"status": "pending", "message": str(exc)}
        result = state()
        if applied is not None:
            result["catalogRefresh"] = applied
        return result

    return router
