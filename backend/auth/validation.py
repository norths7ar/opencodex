"""Pre-save validation of a newly issued ChatGPT grant."""

import asyncio
import time
from collections.abc import Callable

from backend.auth.access import ChannelAuthentication, validate_subscription
from backend.configuration.models import Channel
from backend.protocols.sse import events
from backend.providers.chatgpt import discovery_version, subscription_body
from backend.providers.endpoints import discovery_url, generation_endpoint


class SubscriptionValidator:
    """Validate a candidate grant without reading or replacing the active account.

    channel() returns the trusted subscription channel; client_for(channel)
    returns an existing appropriately scoped AsyncClient. set_stage is optional
    and receives only the structural stage name, never credentials or content.
    """

    def __init__(
        self,
        channel: Callable[[], Channel],
        authentication: ChannelAuthentication,
        client_for: Callable,
        *,
        set_stage: Callable[[str], None] | None = None,
        clock=time.time,
    ):
        self.channel, self.authentication, self.client_for = channel, authentication, client_for
        self.set_stage, self.clock = set_stage, clock

    def _stage(self, value):
        if self.set_stage:
            self.set_stage(value)

    async def __call__(self, credential):
        channel = self.channel()
        validate_subscription(channel)
        client = self.client_for(channel)
        headers = self.authentication.headers(channel, "openai-responses", credential=credential)
        self._stage("models")
        async with asyncio.timeout(60):
            roster = await client.get(
                discovery_url(channel),
                params={
                    "client_version": discovery_version(
                        channel.request_policy.modelDiscoveryClientVersion
                    )
                },
                headers=headers,
            )
            if roster.status_code != 200:
                raise ValueError(
                    f"ChatGPT account model access could not be verified (HTTP {roster.status_code}). The previous account was kept."
                )
            payload = roster.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise TypeError("ChatGPT account returned an invalid model directory.")
            candidates = [
                row["slug"]
                for row in payload["models"]
                if isinstance(row, dict)
                and isinstance(row.get("slug"), str)
                and row.get("supported_in_api") is not False
                and row.get("visibility") != "hide"
            ]
            if not candidates:
                raise ValueError(
                    "ChatGPT login succeeded, but the account returned no usable models. Check its subscription access."
                )
            preferred = [model for model in candidates if "luna" in model or "mini" in model]
            for model in list(dict.fromkeys(preferred + candidates))[:3]:
                self._stage("validation")
                body = subscription_body(
                    {
                        "model": model,
                        "input": "Reply with OK only.",
                        "instructions": "Reply briefly.",
                    },
                    installation_id=credential.get("installationId"),
                )
                async with client.stream(
                    "POST", generation_endpoint(channel, model).url, headers=headers, json=body
                ) as response:
                    if response.status_code in {400, 404}:
                        continue
                    if response.status_code == 429:
                        raise ValueError(
                            "ChatGPT login succeeded, but the account usage limit prevented validation. Retry after its limit resets."
                        )
                    if response.status_code != 200:
                        raise ValueError(
                            f"ChatGPT account validation failed (HTTP {response.status_code}). The previous account was kept."
                        )
                    async for event in events(response):
                        if event.get("type") == "response.completed":
                            credential["validatedAt"] = self.clock()
                            return
                        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                            raise ValueError(
                                "ChatGPT account validation did not complete. The previous account was kept."
                            )
                    raise ValueError("ChatGPT account validation stream ended before completion.")
            raise ValueError(
                "ChatGPT login succeeded, but available models refused the validation request. Check account provisioning."
            )
