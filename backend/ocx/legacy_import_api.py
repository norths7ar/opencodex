"""Explicit preview/apply entry point for original provider and model configuration."""

import inspect

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .legacy_provider_import import LegacyProviderImport


class ImportSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(min_length=1)


class ImportSelection(ImportSource):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected: list[str] = Field(min_length=1)


def legacy_import_router(store, on_applied=None):
    router = APIRouter(prefix="/api/provider-import")
    importer = LegacyProviderImport(store)

    @router.post("/preview")
    def preview(body: ImportSource):
        try:
            return importer.preview(body.source)
        except OSError, ValueError, TypeError, RuntimeError:
            raise HTTPException(
                400, "Cannot preview this original provider configuration."
            ) from None

    @router.post("/apply")
    async def apply(body: ImportSelection):
        try:
            result = importer.apply(body.source, body.digest, body.selected)
        except OSError, ValueError, TypeError, RuntimeError:
            raise HTTPException(
                409,
                "Import could not be applied. Preview the source again and review the selection.",
            ) from None
        result["catalogRefreshPending"] = False
        if on_applied:
            try:
                pending = on_applied(result["imported"])
                if inspect.isawaitable(pending):
                    await pending
            except Exception:  # noqa: BLE001 - refresh failure must not misreport a committed import
                # The saved import remains successful even if discovery is temporarily offline.
                result["catalogRefreshPending"] = True
        return result

    return router
