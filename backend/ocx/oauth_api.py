"""Original console OAuth contracts backed by one account per subscription channel."""

from fastapi import APIRouter, HTTPException, Request

from .config import Provider
from .subscription_channels import CHANNELS, LOGIN_FLOWS


def oauth_router(store, channels, quota, kimi_quota):
    router = APIRouter()

    def supported(provider):
        if provider not in channels:
            raise HTTPException(400, "This subscription channel has not been migrated yet.")
        return channels[provider]

    def quota_for(provider):
        if provider == "openai":
            return quota
        if provider in {"kimi", "kimi-responses"}:
            return kimi_quota
        return None

    @router.get("/oauth/providers")
    def providers():
        return {"providers": list(channels)}

    @router.get("/oauth/status")
    def status(provider: str):
        auth = supported(provider)
        value = auth.status()
        provider_quota = quota_for(provider)
        if provider_quota:
            for row in value["accounts"]:
                row.update(provider_quota.account_fields(row["id"]))
        return value

    @router.get("/oauth/accounts")
    async def accounts(request: Request, provider: str, refresh: str = ""):
        auth = supported(provider)
        enriched = request.query_params.get("quota") in {"1", "true"}
        provider_quota = quota_for(provider)
        if provider_quota and enriched:
            options = {"account": True} if provider in {"kimi", "kimi-responses"} else {}
            await provider_quota.read(force=refresh in {"1", "true"}, **options)
        value = auth.status()
        if provider_quota:
            for row in value["accounts"]:
                row.update(provider_quota.account_fields(row["id"], enriched=enriched))
        return {
            "accounts": value["accounts"],
            "activeAccountId": value["activeAccountId"],
            "revision": auth.revision,
        }

    @router.post("/oauth/login")
    async def login(body: dict):
        provider = body.get("provider")
        auth = supported(provider)
        flows = LOGIN_FLOWS[provider]
        flow = body.get("flow", flows[0])
        if flow not in flows:
            raise ValueError("Choose a supported login flow for this channel.")
        target = body.get("accountId")
        if target is not None or body.get("reauth") is True:
            if not isinstance(target, str) or not target.strip():
                raise ValueError("Select the subscription account to reauthorize.")
            target = target.strip()
        with store.mutation_lock:
            config = store.load()
            existing = next((p for p in config.providers if p.id == provider), None)
            if existing and existing.authMode != "oauth":
                raise ValueError(
                    "This connection already uses API credentials; keep it as a separate API channel before adding the subscription."
                )
            if existing is None:
                config.providers.append(
                    Provider(
                        id=provider,
                        name=provider,
                        authMode="oauth",
                        **CHANNELS[provider],
                    )
                )
                if config.defaultProvider is None:
                    config.defaultProvider = provider
                store.save(config)
        options = (
            {"code_mode": flow == "code"}
            if provider == "anthropic"
            else {"device": flow == "device"}
        )
        return await auth.start(
            **options,
            open_browser=provider not in {"kimi", "kimi-responses"}
            and body.get("openBrowser", True) is True,
            reauth_account_id=target,
        )

    @router.post("/oauth/login/cancel")
    async def cancel(body: dict):
        auth = supported(body.get("provider"))
        await auth.cancel()
        return {"ok": True}

    @router.post("/oauth/login/code")
    async def code(body: dict):
        auth = supported(body.get("provider"))
        value = body.get("code", body.get("input"))
        if not isinstance(value, str):
            raise HTTPException(400, "Paste the complete callback URL.")
        auth.submit(value.strip())
        return {"ok": True, "success": True}

    @router.post("/oauth/logout")
    async def logout(provider: str):
        auth = supported(provider)
        await auth.logout()
        return {"ok": True}

    @router.delete("/oauth/accounts")
    async def delete(provider: str, id: str):
        auth = supported(provider)
        if auth.status()["activeAccountId"] != id:
            raise HTTPException(404, "Account not found.")
        await auth.logout()
        return {"ok": True}

    @router.put("/oauth/accounts/alias")
    async def alias(body: dict):
        auth = supported(body.get("provider"))
        value = body.get("alias", "")
        if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
            raise ValueError("Use an account label of at most 80 printable characters.")
        async with auth.lock:
            saved = auth.load()
            if not saved or saved["accountId"] != body.get("accountId", body.get("id")):
                raise HTTPException(404, "Account not found.")
            auth.save({**saved, "alias": value.strip() or None})
        return {"ok": True, "alias": value.strip() or None}

    return router
