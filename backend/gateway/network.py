"""Address-pinned HTTP client preserving request host and TLS server name.

Use one client per destination policy. Environment HTTP(S) proxy selection is
honored; proxy CONNECT targets the validated IP while TLS remains bound to the
original hostname. Automatic redirects are disabled and must be revalidated.
"""

import base64
import ipaddress
import re
import socket
import ssl
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

import anyio
import httpcore
import httpx
from httpx._transports.default import AsyncResponseStream, map_httpcore_exceptions

from backend.providers.destination_policy import validate_destination


def _canonical_chatgpt_url(url):
    if (
        url.scheme != "https"
        or url.host != "chatgpt.com"
        or url.port not in {None, 443}
        or url.userinfo
        or url.fragment
    ):
        return False
    path = url.raw_path.partition(b"?")[0]
    if path == b"/backend-api/codex/models":
        return not url.query or re.fullmatch(
            rb"client_version=[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", url.query
        )
    return (
        path in {b"/backend-api/codex/responses", b"/backend-api/codex/responses/compact"}
        and not url.query
    )


async def system_resolver(host):
    records = await anyio.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(record[4][0] for record in records))


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        *,
        allow_private=False,
        resolver=system_resolver,
        proxy=None,
        backend=None,
        proxy_ssl=None,
        allow_benchmark=False,
        allow_mihomo=False,
    ):
        self.allow_private, self.resolver, self.proxy = allow_private, resolver, proxy
        self.backend = backend or httpcore.AnyIOBackend()
        self.proxy_ssl = proxy_ssl or ssl.create_default_context()
        self.allow_benchmark = allow_benchmark
        self.allow_mihomo = allow_mihomo

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        host_text = f"[{host}]" if ":" in host else host
        addresses = await validate_destination(
            f"https://{host_text}:{port}",
            allow_private=self.allow_private,
            resolver=self.resolver,
            allow_benchmark=self.allow_benchmark,
            allow_mihomo=self.allow_mihomo,
        )
        failure = None
        for address in addresses:
            try:
                if not self.proxy:
                    return await self.backend.connect_tcp(
                        address,
                        port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                return await self._tunnel(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                failure = exc
        if failure:
            raise failure
        raise httpcore.ConnectError("No validated address available.")

    async def _tunnel(self, address, port, timeout, local_address, socket_options):
        parsed = urlsplit(self.proxy)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise httpcore.ProxyError("Unsupported proxy URL.")
        stream = await self.backend.connect_tcp(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        try:
            if parsed.scheme == "https":
                stream = await stream.start_tls(self.proxy_ssl, parsed.hostname, timeout=timeout)
            ip = ipaddress.ip_address(address)
            authority = f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"
            lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
            if parsed.username is not None:
                credentials = (
                    f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()
                )
                lines.append(
                    "Proxy-Authorization: Basic " + base64.b64encode(credentials).decode("ascii")
                )
            await stream.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"), timeout=timeout)
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                byte = await stream.read(1, timeout=timeout)
                if not byte or len(header) >= 16384:
                    raise httpcore.ProxyError("Invalid proxy response.")
                header.extend(byte)
            first = bytes(header).split(b"\r\n", 1)[0].split(b" ", 2)
            if len(first) < 2 or not first[1].isdigit() or not 200 <= int(first[1]) < 300:
                raise httpcore.ProxyError("Proxy tunnel was refused.")
            return stream
        except BaseException:
            await stream.aclose()
            raise

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not outbound provider destinations.")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        network_backend,
        *,
        verify=True,
        limits=None,
        forward_transport=None,
        transparent_fake_ip=False,
    ):
        limits = limits or httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=5
        )
        context = ssl.create_default_context() if verify is True else verify
        if context is False:
            context = ssl._create_unverified_context()
        self.network = network_backend
        self.forward = forward_transport or (
            httpx.AsyncHTTPTransport(proxy=network_backend.proxy, verify=context)
            if network_backend.proxy
            else None
        )

        def pool(network):
            return httpcore.AsyncConnectionPool(
                ssl_context=context,
                network_backend=network,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
            )

        self.pool = pool(network_backend)
        # A connection admitted for a fixed route must not be reused for another path.
        self.canonical_pool = (
            pool(
                PinnedBackend(
                    allow_private=network_backend.allow_private,
                    resolver=network_backend.resolver,
                    proxy=network_backend.proxy,
                    backend=network_backend.backend,
                    proxy_ssl=network_backend.proxy_ssl,
                    allow_benchmark=True,
                    allow_mihomo=True,
                )
            )
            if transparent_fake_ip
            else None
        )

    async def handle_async_request(self, request):
        if request.url.scheme == "http" and self.forward is not None:
            addresses = await validate_destination(
                str(request.url),
                allow_private=self.network.allow_private,
                resolver=self.network.resolver,
                allow_benchmark=self.network.allow_benchmark,
                allow_mihomo=self.network.allow_mihomo,
            )
            pinned = request.url.copy_with(host=addresses[0])
            forwarded = httpx.Request(
                request.method,
                pinned,
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
            return await self.forward.handle_async_request(forwarded)
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            pool = (
                self.canonical_pool
                if self.canonical_pool is not None and _canonical_chatgpt_url(request.url)
                else self.pool
            )
            response = await pool.handle_async_request(core)
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=AsyncResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        try:
            await self.pool.aclose()
        finally:
            try:
                if self.canonical_pool is not None:
                    await self.canonical_pool.aclose()
            finally:
                if self.forward is not None:
                    await self.forward.aclose()


def build_client(
    url: str,
    *,
    allow_private=False,
    resolver=system_resolver,
    trust_env=True,
    proxy=None,
    backend=None,
    timeout=None,
):
    """Create a pinned client for one URL's scheme and proxy selection.

    The caller must use one client per channel/allow-private policy; redirect
    following remains disabled. Explicit proxy overrides environment selection.
    """
    parsed = urlsplit(url)
    selected = proxy
    bypass = bool(trust_env and parsed.hostname and proxy_bypass(parsed.hostname))
    binding = proxy
    if selected is None and trust_env and parsed.hostname and not bypass:
        proxies = getproxies()
        binding = proxies.get(parsed.scheme)
        selected = binding or proxies.get("all")
    network = PinnedBackend(
        allow_private=allow_private,
        resolver=resolver,
        proxy=selected,
        backend=backend,
        allow_benchmark=bool(selected),
        allow_mihomo=bool(binding),
    )
    return httpx.AsyncClient(
        transport=PinnedTransport(
            network,
            transparent_fake_ip=parsed.hostname == "chatgpt.com"
            and (not bypass or proxy is not None),
        ),
        timeout=timeout or httpx.Timeout(120, connect=10),
        follow_redirects=False,
        trust_env=trust_env,
    )
