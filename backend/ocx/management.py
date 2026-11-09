"""Management endpoints, mounted behind the runtime's local-session boundary."""

from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import clients
from .config import ConfigStore, Configuration
from .gateway import Gateway


class CheckRequest(BaseModel):
    model: str


class ClientRequest(BaseModel):
    home: str | None = None


def management_router(store: ConfigStore, gateway: Gateway, directory: Path, port: int):
    router = APIRouter(prefix="/api")
    base_url = f"http://127.0.0.1:{port}/v1"

    @router.get("/config")
    def config():
        return store.public()

    @router.put("/config")
    def save_config(value: Configuration):
        return store.save(value)

    @router.get("/access")
    def access():
        return {"baseUrl": base_url, "apiKey": store.gateway_key}

    @router.post("/providers/{provider_id}/models")
    async def models(provider_id: str):
        try:
            return {"models": await gateway.models(store.provider(provider_id))}
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Model discovery could not reach the upstream.") from exc

    @router.post("/providers/{provider_id}/check")
    async def check(provider_id: str, value: CheckRequest):
        try:
            return await gateway.check(store.provider(provider_id), value.model)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "The upstream connection check failed.") from exc

    def default_model():
        config = store.load()
        if not config.defaultProvider or not config.defaultModel:
            raise ValueError("Save a default provider and model before configuring Codex.")
        provider = store.provider(config.defaultProvider)
        if not provider.responses:
            raise ValueError(
                "Codex requires a provider with native Responses support in this migration stage."
            )
        return f"{config.defaultProvider}/{config.defaultModel}"

    @router.get("/clients/codex")
    def codex_preview(home: str | None = None):
        config = store.load()
        model = (
            f"{config.defaultProvider}/{config.defaultModel}"
            if config.defaultProvider and config.defaultModel
            else ""
        )
        return clients.preview(clients.codex_home(home), base_url, model, directory)

    @router.post("/clients/codex/configure")
    def configure_codex(value: ClientRequest):
        return clients.configure(
            clients.codex_home(value.home), base_url, default_model(), store.gateway_key, directory
        )

    @router.post("/clients/codex/restore")
    def restore_codex(value: ClientRequest):
        return clients.restore(clients.codex_home(value.home), directory)

    return router
