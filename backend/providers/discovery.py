"""Fetch a complete upstream directory; scheduling and persistence live elsewhere."""

import httpx

from backend.auth.access import ChannelAuthentication
from backend.configuration.models import Channel
from backend.providers.endpoints import discovery_url


class ModelDiscovery:
    def __init__(
        self,
        client: httpx.AsyncClient,
        authentication: ChannelAuthentication,
        *,
        client_version: str = "0.159.0",
        client_for_channel=None,
    ):
        self.client = client
        self.authentication = authentication
        self.client_version = client_version
        self.client_for_channel = client_for_channel

    async def __call__(self, channel: Channel) -> list[dict]:
        subscription = channel.auth_mode == "oauth"
        credential = await self.authentication.token(channel) if subscription else None
        params = {}
        if subscription:
            params["client_version"] = (
                channel.request_policy.modelDiscoveryClientVersion or self.client_version
            )
        address = discovery_url(channel)
        client = (
            self.client_for_channel(channel, address) if self.client_for_channel else self.client
        )
        rows: dict[str, dict] = {}
        cursors: set[str] = set()
        while True:
            for attempt in (0, 1):
                response = await client.get(
                    address,
                    params=params,
                    headers=self.authentication.headers(
                        channel,
                        channel.protocol,
                        credential=credential,
                        discovery=True,
                    ),
                )
                if response.status_code != 401 or not subscription or attempt:
                    break
                credential = await self.authentication.token(channel, rejected=credential)
            if response.status_code != 200:
                raise ValueError(
                    f"Model discovery failed with upstream HTTP {response.status_code}."
                )
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("Upstream did not return a model list.")
            data = payload.get("models" if subscription else "data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise ValueError("Upstream did not return a model list.")
            if subscription:
                data = [
                    {**row, "id": row.get("slug"), "name": row.get("display_name", row.get("slug"))}
                    for row in data
                    if row.get("supported_in_api") is not False and row.get("visibility") != "hide"
                ]
            if any(not isinstance(row.get("id"), str) or not row["id"].strip() for row in data):
                raise ValueError("Upstream did not return a model list.")
            rows.update((row["id"], row) for row in data)
            if not payload.get("has_more") and not payload.get("nextPageToken"):
                return list(rows.values())
            cursor = payload.get("last_id")
            if (
                channel.protocol != "anthropic"
                or payload.get("nextPageToken")
                or not isinstance(cursor, str)
                or not cursor
                or cursor in cursors
                or not data
            ):
                raise ValueError("Upstream model pagination is unsupported or did not advance.")
            cursors.add(cursor)
            params["after_id"] = cursor
