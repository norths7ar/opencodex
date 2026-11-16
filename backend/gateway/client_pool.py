"""Own HTTP connection pools keyed by channel, destination and network policy."""

from urllib.parse import urlsplit

from .network import build_client


class UpstreamClients:
    def __init__(self, *, override=None):
        self.override = override
        self.clients = {}

    def get(self, url, *, channel_id="public", allow_private=False):
        if self.override is not None:
            return self.override
        address = urlsplit(url)
        key = channel_id, address.scheme, address.hostname, address.port, allow_private
        if key not in self.clients:
            self.clients[key] = build_client(url, allow_private=allow_private)
        return self.clients[key]

    def for_channel(self, channel, url):
        return self.get(url, channel_id=channel.id, allow_private=channel.allow_private_network)

    def for_send(self, prepared):
        return self.get(
            prepared.url, channel_id=prepared.channel_id, allow_private=prepared.allow_private
        )

    async def close(self):
        if self.override is not None:
            await self.override.aclose()
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
