"""Management projection of the active ChatGPT subscription's measured quota."""

from fastapi import APIRouter

from .quota import MAX_REPORT_AGE_MS, now_ms, provider_quota


def quota_router(store, quota, kimi_quota=None):
    router = APIRouter()

    @router.get("/provider-quotas")
    async def reports(refresh: str = ""):
        provider = next(
            (
                p
                for p in store.load().providers
                if p.id == "openai" and p.authMode == "oauth" and not p.disabled
            ),
            None,
        )
        state = await quota.read(force=refresh in {"1", "true"}) if provider else None
        now = now_ms()
        result = {"generatedAt": now, "reports": []}
        kimi_channels = [
            p
            for p in store.load().providers
            if p.id in {"kimi", "kimi-responses"} and p.authMode == "oauth" and not p.disabled
        ]
        if kimi_quota and kimi_channels:
            kimi_state = await kimi_quota.read(force=refresh in {"1", "true"})
            if kimi_state and kimi_state.quota:
                result["reports"].extend(
                    {
                        "provider": channel.id,
                        "label": "Kimi",
                        "source": "kimi:usages",
                        "updatedAt": kimi_state.quota["updatedAt"],
                        "quota": kimi_state.quota,
                    }
                    for channel in kimi_channels
                )
            if not provider:
                result["quotaProvider"] = kimi_channels[0].id
                if kimi_state and kimi_state.checked_at:
                    result["checkedAt"] = kimi_state.checked_at
                if not kimi_state or kimi_state.failure or not kimi_state.quota:
                    result["quotaUnavailable"] = True
                    if kimi_state and kimi_state.failure:
                        result["quotaFailure"] = kimi_state.failure
        if provider:
            result["quotaProvider"] = provider.id
        if state is None:
            return result
        projected = provider_quota(state.quota)
        if projected and now - projected["updatedAt"] < MAX_REPORT_AGE_MS:
            result["reports"].append(
                {
                    "provider": provider.id,
                    "label": "ChatGPT",
                    "source": "chatgpt:wham",
                    "updatedAt": projected["updatedAt"],
                    "quota": projected,
                }
            )
        if state.checked_at:
            result["checkedAt"] = state.checked_at
        if state.failure or not result["reports"]:
            result["quotaUnavailable"] = True
            if state.failure:
                result["quotaFailure"] = state.failure
        return result

    return router
