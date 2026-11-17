"""Outbound destination checks; apply to initial URL and every redirect target.

Resolve at the transport boundary and bind the connection to a checked address to
avoid a DNS rebinding gap. This module never performs DNS itself.
"""

import ipaddress
from collections.abc import Awaitable, Callable, Iterable
from typing import cast
from urllib.parse import urljoin, urlsplit

_METADATA_HOSTS = {
    "instance-data.ec2.internal",
    "metadata.azure.internal",
    "metadata.google.internal",
}
_METADATA_IPS = {
    ipaddress.ip_address(v)
    for v in ("100.100.100.200", "169.254.169.254", "169.254.170.2", "fd00:ec2::254")
}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_BENCHMARK = ipaddress.ip_network("198.18.0.0/15")
_EXPLICIT_ZERO_MAPPED = ipaddress.ip_network("::ffff:0:0:0/96")
_MIHOMO_FAKE_IP = ipaddress.ip_network("fdfe:dcba:9876::/48")
_PRIVATE_V4 = tuple(
    ipaddress.ip_network(v)
    for v in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)


class DestinationDenied(ValueError):
    pass


def classify(address: str) -> str:
    host = address.lower().strip().rstrip(".")
    if host in _METADATA_HOSTS:
        return "metadata"
    if host == "localhost" or host.endswith(".localhost"):
        return "localhost"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "hostname"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return classify(str(ip.ipv4_mapped))
        if ip in _NAT64:
            return classify(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    if ip in _METADATA_IPS:
        return "metadata"
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_link_local:
        return "link-local"
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in network for network in _PRIVATE_V4):
            return "private"
        return "public"
    if (
        ip.is_multicast
        or ip in ipaddress.ip_network("fc00::/7")
        or ip in ipaddress.ip_network("fec0::/10")
        or ip in ipaddress.ip_network("2001:db8::/32")
        or ip not in ipaddress.ip_network("2000::/3")
    ):
        return "private"
    return "public"


def validate_literal(url: str, *, allow_private=False) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            raise ValueError()
    except ValueError:
        raise DestinationDenied("Invalid outbound destination.") from None
    kind = classify(host)
    if kind == "metadata":
        raise DestinationDenied("Metadata endpoints are blocked.")
    if kind in {"link-local", "unspecified"} or (
        kind not in {"public", "hostname"} and not allow_private
    ):
        raise DestinationDenied("Private outbound destination requires explicit opt-in.")
    return kind


def _fake_dns_answer(address, *, allow_benchmark, allow_mihomo):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv4Address):
        return allow_benchmark and ip in _BENCHMARK
    if allow_mihomo and ip in _MIHOMO_FAKE_IP:
        return True
    if allow_benchmark and (ip.ipv4_mapped or ip in _NAT64 or ip in _EXPLICIT_ZERO_MAPPED):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) in _BENCHMARK
    return False


async def validate_destination(
    url: str,
    *,
    allow_private=False,
    resolver: Callable[[str], Awaitable[Iterable[str]]],
    allow_benchmark=False,
    allow_mihomo=False,
) -> tuple[str, ...]:
    kind = validate_literal(url, allow_private=allow_private)
    if kind not in {"hostname", "localhost"}:
        return (cast(str, urlsplit(url).hostname),)
    host = cast(str, urlsplit(url).hostname)
    addresses = tuple(await resolver(host))
    if not addresses:
        raise DestinationDenied("Destination did not resolve.")
    for address in addresses:
        resolved = classify(address)
        if resolved in {"hostname", "metadata"}:
            raise DestinationDenied("Blocked or invalid DNS answer.")
        if resolved == "private" and _fake_dns_answer(
            address, allow_benchmark=allow_benchmark, allow_mihomo=allow_mihomo
        ):
            continue
        if resolved in {"link-local", "unspecified"} or (
            resolved != "public" and not allow_private
        ):
            raise DestinationDenied("Private DNS answer requires explicit opt-in.")
    return addresses


async def validate_redirect(base_url: str, location: str, *, allow_private=False, resolver):
    target = urljoin(base_url, location)
    addresses = await validate_destination(target, allow_private=allow_private, resolver=resolver)
    return target, addresses
