"""HTTP and event-stream framing for single-account ChatGPT management."""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from backend.application.subscriptions import SubscriptionManagement


def subscription_router(channels, *, quota=None):
    router = APIRouter()
    service = SubscriptionManagement(channels, quota=quota)
    service.lock = channels.login_lock

    @router.get("/oauth/providers")
    def providers():
        return service.oauth_providers()

    @router.get("/oauth/status")
    def status(provider: str):
        return service.oauth_status(provider)

    @router.get("/oauth/accounts")
    async def accounts(provider: str, quota: bool = False, refresh: bool = False):
        return await service.accounts(provider, refresh, enriched=quota)

    @router.post("/oauth/login")
    async def login(body: dict):
        return await service.login(body)

    @router.post("/oauth/login/cancel")
    async def cancel(body: dict):
        return await service.cancel(body)

    @router.post("/oauth/login/code")
    async def code(body: dict):
        return await service.login_code(body)

    @router.post("/oauth/logout")
    async def logout(provider: str):
        return await service.logout(provider)

    @router.delete("/oauth/accounts")
    async def remove(provider: str, id: str):
        return await service.delete_account(provider, id)

    @router.put("/oauth/accounts/alias")
    async def alias(body: dict):
        return await service.account_alias(body)

    @router.get("/provider-quotas")
    async def quotas(refresh: bool = False):
        return await service.provider_quotas(refresh)

    @router.get("/accounts/events")
    async def events(request: Request):
        async def stream():
            revision = service.auth.revision
            yield f"event: ready\ndata: {json.dumps({'revision': revision})}\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(1)
                if revision != service.auth.revision:
                    revision = service.auth.revision
                    channel = next(
                        (
                            c
                            for c in channels.store.read().configuration.channels
                            if c.auth_mode == "oauth"
                        ),
                        None,
                    )
                    payload = {
                        "provider": channel.id if channel else "chatgpt",
                        "kind": "oauth",
                        "revision": revision,
                    }
                    yield f"event: account-selection\ndata: {json.dumps(payload)}\n\n"
                else:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
