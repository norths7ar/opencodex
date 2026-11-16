"""Single-account subscription lifecycle and quota queries."""

import asyncio

from backend.auth.access import is_chatgpt_subscription
from backend.auth.quota import MAX_REPORT_AGE_MS, now_ms, provider_quota
from backend.configuration.models import Channel, Endpoints
from backend.providers import SERVICES

from .contracts import SaveChannel
from .errors import ApplicationError


class SubscriptionManagement:
    def __init__(self, channels, *, quota=None):
        self.channels, self.auth, self.quota = (channels, channels.auth, quota)
        self.lock = asyncio.Lock()

    def supported(self, provider):
        if not isinstance(provider, str):
            raise ApplicationError(400, "Select a subscription service.")
        if provider not in {"chatgpt", "openai"}:
            config = self.channels.store.read().configuration
            if not any(
                value.id == provider and is_chatgpt_subscription(value) for value in config.channels
            ):
                raise ApplicationError(400, "This subscription service is not supported.")

    def oauth_providers(self):
        return {
            "providers": list(
                dict.fromkeys(
                    [
                        "chatgpt",
                        "openai",
                        *(
                            value.id
                            for value in self.channels.store.read().configuration.channels
                            if is_chatgpt_subscription(value)
                        ),
                    ]
                )
            )
        }

    def oauth_status(self, provider: str):
        self.supported(provider)
        value = self.auth.status()
        if self.quota:
            for account in value.get("accounts", []):
                account.update(self.quota.account_fields(account["id"]))
        return value

    async def accounts(self, provider: str, refresh: bool = False, *, enriched: bool = False):
        self.supported(provider)
        if self.quota and enriched:
            await self.quota.read(force=refresh)
        value = self.auth.status()
        if self.quota:
            for account in value.get("accounts", []):
                account.update(self.quota.account_fields(account["id"], enriched=enriched))
        return {
            "accounts": value.get("accounts", []),
            "activeAccountId": value.get("activeAccountId"),
            "revision": self.auth.revision,
        }

    async def login(self, body: dict):
        provider = body.get("provider")
        self.supported(provider)
        mode = body.get("flow", "browser")
        account_id = body.get("accountId")
        if mode not in {"browser", "device"}:
            raise ApplicationError(400, "Choose browser or device login.")
        if (account_id is not None or body.get("reauth") is True) and (
            not (isinstance(account_id, str) and account_id.strip())
        ):
            raise ApplicationError(400, "Select the account to reauthorize.")
        async with self.lock:
            state = self.channels.store.read()
            if not any(map(is_chatgpt_subscription, state.configuration.channels)):
                name = "openai" if provider in {"openai", "chatgpt"} else provider
                if any(value.id == name for value in state.configuration.channels):
                    raise ApplicationError(
                        409, "This channel ID already belongs to an API connection."
                    )
                created = Channel.model_validate(
                    {
                        "id": name,
                        "name": "ChatGPT",
                        "service_id": "chatgpt",
                        "protocol": "openai-responses",
                        "auth_mode": "oauth",
                        "oauth_binding": {"service_id": "chatgpt", "account_ref": "chatgpt"},
                        "endpoints": Endpoints(
                            base_url=next(s for s in SERVICES if s.id == "chatgpt")
                            .destinations[0]
                            .base_url
                        ),
                    }
                )
                await self.channels.save(SaveChannel(channel=created), revision=state.revision)
            try:
                return await self.auth.start(
                    device=mode == "device",
                    open_browser=body.get("openBrowser", True) is True,
                    reauth_account_id=account_id.strip() if account_id else None,
                )
            except (ValueError, RuntimeError) as exc:
                raise ApplicationError(400, str(exc)) from exc

    async def cancel(self, body: dict):
        self.supported(body.get("provider"))
        await self.auth.cancel()
        return {"ok": True}

    async def login_code(self, body: dict):
        self.supported(body.get("provider"))
        callback = body.get("code", body.get("input"))
        if not isinstance(callback, str):
            raise ApplicationError(400, "Paste the complete callback URL.")
        try:
            self.auth.submit(callback.strip())
        except ValueError as exc:
            raise ApplicationError(400, str(exc)) from exc
        return {"ok": True, "success": True}

    async def logout(self, provider: str):
        self.supported(provider)
        await self.auth.logout()
        self.channels.catalog.request_refresh()
        return {"ok": True}

    async def delete_account(self, provider: str, id: str):
        self.supported(provider)
        if self.auth.status().get("activeAccountId") != id:
            raise ApplicationError(404, "Account not found.")
        return await self.logout(provider)

    async def account_alias(self, body: dict):
        self.supported(body.get("provider"))
        label = body.get("alias", "")
        if not isinstance(label, str) or len(label) > 80 or (label and (not label.isprintable())):
            raise ApplicationError(400, "Use a printable label of at most 80 characters.")
        async with self.auth.lock:
            saved = self.auth.load()
            if not saved or saved["accountId"] != body.get("accountId", body.get("id")):
                raise ApplicationError(404, "Account not found.")
            self.auth.save({**saved, "alias": label.strip() or None})
        return {"ok": True, "alias": label.strip() or None}

    async def provider_quotas(self, refresh: bool = False):
        value = next(
            (
                value
                for value in self.channels.store.read().configuration.channels
                if is_chatgpt_subscription(value) and not value.disabled
            ),
            None,
        )
        now = now_ms()
        result = {"generatedAt": now, "reports": []}
        if value is None or self.quota is None:
            return result
        result["quotaProvider"] = value.id
        snapshot = await self.quota.read(force=refresh)
        if snapshot is None:
            return result
        projected = provider_quota(snapshot.quota)
        if projected and now - projected["updatedAt"] < MAX_REPORT_AGE_MS:
            result["reports"].append(
                {
                    "provider": value.id,
                    "label": "ChatGPT",
                    "source": "chatgpt:wham",
                    "updatedAt": projected["updatedAt"],
                    "quota": projected,
                }
            )
        if snapshot.checked_at:
            result["checkedAt"] = snapshot.checked_at
        if snapshot.failure or not result["reports"]:
            result["quotaUnavailable"] = True
            if snapshot.failure:
                result["quotaFailure"] = snapshot.failure
        return result
