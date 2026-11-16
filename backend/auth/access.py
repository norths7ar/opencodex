"""Resolve credentials for a channel without borrowing downstream credentials."""

from collections.abc import Mapping
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

import httpx

from backend.configuration.models import Channel, CredentialRecord, Protocol
from backend.configuration.store import ConfigurationStore
from backend.providers.registry import match_destination

from .chatgpt import ChatGPTAuth
from .storage import resolve_credential

SUBSCRIPTION_HEADERS = (
    "openai-beta",
    "originator",
    "session_id",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-parent-thread-id",
    "x-codex-turn-state",
    "x-codex-window-id",
    "x-openai-subagent",
    "x-responsesapi-include-timing-metrics",
    "version",
    "x-openai-internal-codex-responses-lite",
)


class SubscriptionAuthorizationError(ValueError):
    status = 401
    code = "authentication_required"


def validate_subscription(channel: Channel):
    match = match_destination(channel)
    binding = channel.oauth_binding
    if (
        match is None
        or match.service.id != "chatgpt"
        or channel.service_id != "chatgpt"
        or binding is None
        or binding.service_id != "chatgpt"
        or binding.account_ref != "chatgpt"
        or channel.protocol != "openai-responses"
        or any(protocol != "openai-responses" for protocol in channel.models.adapters.values())
        or any(
            getattr(channel.endpoints, field)
            for field in ("discovery", "chat", "responses", "messages")
        )
    ):
        raise ValueError("ChatGPT authorization requires its subscription connection.")


class ChannelAuthentication:
    def __init__(
        self, store: ConfigurationStore, chatgpt: ChatGPTAuth, *, dotenv_path: Path | None = None
    ):
        self.store = store
        self.chatgpt = chatgpt
        self.dotenv_path = dotenv_path

    def record(self, channel: Channel) -> CredentialRecord:
        return self.store.read().credentials.get(channel.id, CredentialRecord())

    def revision(self, channel: Channel) -> str:
        """Directory identity changes with credentials, not ordinary token refreshes."""
        record = self.record(channel)
        identity = record.protected_key or record.env_var
        if channel.auth_mode == "oauth":
            identity = self.chatgpt.status().get("activeAccountId")
        elif channel.auth_mode == "key":
            with suppress(ValueError, RuntimeError):
                identity, _ = resolve_credential(record, dotenv_path=self.dotenv_path)
        return sha256(repr((identity, sorted(record.headers.items()))).encode()).hexdigest()

    def legacy_fingerprint(self, channel: Channel) -> str:
        """Verify an existing v1 directory without requesting its models again."""
        record = self.record(channel)
        identity = record.protected_key or record.env_var
        if channel.auth_mode in {"key", "local"}:
            with suppress(ValueError, RuntimeError):
                identity, _ = resolve_credential(record, dotenv_path=self.dotenv_path)
        account = (
            self.chatgpt.status().get("activeAccountId") if channel.auth_mode == "oauth" else None
        )
        values = (
            channel.endpoints.base_url,
            channel.protocol,
            channel.auth_mode,
            identity,
            record.headers,
            channel.api_key_transport,
            channel.endpoints.responses,
            account,
        )
        return sha256(repr(values).encode()).hexdigest()

    async def token(self, channel: Channel, *, rejected=None):
        validate_subscription(channel)
        try:
            return await self.chatgpt.token(
                rejected_access=rejected["access"] if rejected else None,
                expected_account_id=rejected["accountId"] if rejected else None,
            )
        except (ValueError, RuntimeError) as exc:
            raise SubscriptionAuthorizationError(
                "Sign in to this gateway's ChatGPT channel first."
            ) from exc

    def headers(
        self,
        channel: Channel,
        protocol: Protocol,
        *,
        incoming: Mapping[str, str] | None = None,
        credential: dict | None = None,
        discovery: bool = False,
    ) -> dict[str, str]:
        record = self.record(channel)
        incoming = httpx.Headers(incoming or {})
        if channel.auth_mode == "oauth":
            validate_subscription(channel)
            if credential is None:
                raise ValueError("A subscription access token is required.")
            headers = httpx.Headers(record.headers)
            for key in SUBSCRIPTION_HEADERS:
                if incoming.get(key):
                    headers[key] = incoming[key]
            headers.update(
                {
                    "content-type": "application/json",
                    "accept": "text/event-stream",
                    "authorization": f"Bearer {credential['access']}",
                    "chatgpt-account-id": credential["accountId"],
                    "x-codex-installation-id": credential["installationId"],
                }
            )
            headers.pop("x-oai-attestation", None)
            headers.setdefault("originator", "codex_cli_rs")
            return dict(headers)

        key = ""
        if channel.auth_mode == "key":
            key, _ = resolve_credential(record, dotenv_path=self.dotenv_path)
        headers = httpx.Headers({"content-type": "application/json"})
        if protocol == "anthropic":
            headers["anthropic-version"] = incoming.get("anthropic-version", "2023-06-01")
            if incoming.get("anthropic-beta"):
                headers["anthropic-beta"] = incoming["anthropic-beta"]
        transport = channel.api_key_transport
        match = match_destination(channel)
        if transport is None and discovery and match and match.service.id in {"deepseek", "mimo"}:
            transport = "bearer"
        if key:
            if transport == "x-api-key" or (protocol == "anthropic" and transport != "bearer"):
                headers["x-api-key"] = key
            else:
                headers["authorization"] = f"Bearer {key}"
        headers.update(record.headers)
        return dict(headers)
