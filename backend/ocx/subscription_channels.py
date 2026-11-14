"""Canonical channel identities shared by configuration and subscription management."""

from collections.abc import Mapping
from hashlib import sha256
from threading import RLock

from .subscription_history import subscription_issuer

CHANNELS = {
    "openai": {
        "baseUrl": "https://chatgpt.com/backend-api/codex",
        "adapter": "openai-responses",
        "statelessResponses": True,
    },
    "anthropic": {
        "baseUrl": "https://api.anthropic.com",
        "adapter": "anthropic",
    },
    "kimi": {
        "baseUrl": "https://api.kimi.com/coding/v1",
        "adapter": "openai-chat",
        "defaultModel": "kimi-for-coding",
        "oauthId": "kimi",
    },
    "kimi-responses": {
        "baseUrl": "https://api.kimi.com/coding/v1",
        "adapter": "openai-responses",
        "defaultModel": "kimi-for-coding",
        "oauthId": "kimi",
    },
}

LOGIN_FLOWS = {
    "openai": ("browser", "device"),
    "anthropic": ("browser", "code"),
    "kimi": ("device",),
    "kimi-responses": ("device",),
}


class QuotaHandle:
    """A quota dependency that opens its channel only when used."""

    def __init__(self, channels, name):
        self.channels = channels
        self.name = name

    async def read(self, **options):
        return await self.channels.quota_service(self.name).read(**options)

    def invalidate(self):
        return self.channels.quota_service(self.name).invalidate()

    def account_fields(self, account, **options):
        return self.channels.quota_service(self.name).account_fields(account, **options)

    def observe_headers(self, *args, **options):
        return self.channels.quota_service(self.name).observe_headers(*args, **options)


class SubscriptionChannels(Mapping):
    """Own lazily opened accounts; wire aliases share the same account instance."""

    def __init__(self, directory, *, validate=None):
        self._lock = RLock()
        self.directory = directory
        self.validate = validate
        self.on_change = None
        self._auth = {}
        self._quotas = {}
        self._handles = {}

    def __iter__(self):
        return iter(CHANNELS)

    def __len__(self):
        return len(CHANNELS)

    def __contains__(self, name):
        return name in CHANNELS

    def __getitem__(self, name):
        with self._lock:
            if name not in CHANNELS:
                raise KeyError(name)
            identity = CHANNELS[name].get("oauthId", name)
            if identity not in self._auth:
                if identity == "openai":
                    from .oauth import ChatGPTAuth

                    auth = ChatGPTAuth(self.directory)
                    auth.validate = self.validate
                elif identity == "anthropic":
                    from .anthropic_oauth import AnthropicAuth

                    auth = AnthropicAuth(self.directory)
                else:
                    from .kimi_oauth import KimiAuth

                    auth = KimiAuth(self.directory)

                async def changed():
                    if self.on_change:
                        aliases = tuple(
                            key
                            for key, spec in CHANNELS.items()
                            if spec.get("oauthId", key) == identity
                        )
                        await self.on_change(aliases)

                auth.on_change = changed
                self._auth[identity] = auth
            return self._auth[identity]

    def for_provider(self, provider):
        if provider.id in {"kimi", "kimi-responses"}:
            return self["kimi"]
        return self["anthropic" if provider.adapter == "anthropic" else "openai"]

    async def credential(self, provider):
        return await self.for_provider(provider).token() if provider.authMode == "oauth" else None

    async def reject(self, provider, credential):
        if credential and (
            provider.adapter == "anthropic" or provider.id in {"kimi", "kimi-responses"}
        ):
            await self.for_provider(provider).mark_needs_reauth(credential["generation"])

    @staticmethod
    def issuer(provider, credential):
        if not credential:
            return None
        if provider.id in {"kimi", "kimi-responses"}:
            return sha256(f"ocx-kimi-history\0{credential['accountId']}".encode()).hexdigest()
        if provider.adapter == "anthropic":
            return sha256(f"ocx-anthropic-history\0{credential['accountId']}".encode()).hexdigest()
        return subscription_issuer(credential["accountId"])

    def revisions(self):
        with self._lock:
            return {
                name: getattr(self._auth.get(spec.get("oauthId", name)), "revision", 0)
                for name, spec in CHANNELS.items()
            }

    def status(self, name):
        return self[name].status() if name in self else {}

    def quota(self, name):
        with self._lock:
            identity = CHANNELS[name].get("oauthId", name)
            if identity not in {"openai", "kimi"}:
                return None
            if identity not in self._handles:
                self._handles[identity] = QuotaHandle(self, identity)
            return self._handles[identity]

    def quota_service(self, identity):
        with self._lock:
            if identity not in self._quotas:
                if identity == "openai":
                    from .quota import ChatGPTQuota

                    service = ChatGPTQuota(self[identity])
                else:
                    from .kimi_quota import KimiQuota

                    service = KimiQuota(self[identity])
                self._quotas[identity] = service
            return self._quotas[identity]

    async def close(self):
        for service in self._quotas.values():
            close = getattr(service, "close", None)
            if close:
                await close()
        for auth in self._auth.values():
            await auth.close()


def auth_channels(gateway):
    return gateway.channels


def retired_auth_services(channels, removed, providers):
    """A removed wire alias does not retire an account still used by another channel."""
    retained = {
        id(channels[provider.id])
        for provider in providers
        if provider.authMode == "oauth" and provider.id in channels
    }
    return list(
        {
            id(channels[name]): channels[name]
            for name in removed
            if name in channels and id(channels[name]) not in retained
        }.values()
    )
