"""Lite storage has its own data-directory contract, separate from Codex storage."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .storage import Storage


class CleanupRequest(BaseModel):
    digest: str


def storage_router(directory, usage=None):
    router = APIRouter(prefix="/api/lite-storage")
    service = Storage(directory, usage)
    router.service = service

    @router.get("")
    def report():
        return {**service.inventory(), "quarantine": service.list_quarantine()}

    @router.get("/preview")
    def preview():
        return service.preview()

    @router.post("/quarantine")
    def quarantine(body: CleanupRequest):
        try:
            return service.quarantine(body.digest)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/restore/{identity}")
    def restore(identity: str):
        try:
            return service.restore(identity)
        except (ValueError, TypeError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
