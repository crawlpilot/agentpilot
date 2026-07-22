"""Post-DNS-resolution IP validation for the httpx "basic" (non-browser)
tier -- resolves DNS first, then validates *every* resolved IP against the
same deny ranges `agentpilot.egress.policy` enforces for the browser process
(metadata + RFC1918), before connecting.

Checking the hostname string alone ("is this an internal-looking name") is
not enough: an attacker-controlled DNS record can make an innocuous
hostname resolve straight to `169.254.169.254`. The check has to happen
*after* resolution, against the IP that will actually be connected to --
that's the whole DNS-rebinding class of bug this guards.

**Known residual gap**: there is a small TOCTOU window between this
function's own resolution and httpx's independent connect-time resolution
of the same hostname -- a fully rebinding-proof fetcher would pin the
validated IP and connect to it directly (a custom transport overriding
connection establishment), which this pass does not build. Noted here
rather than silently assumed away; closing it fully is future hardening,
not a P2 blocker per `plan.md`'s own scope for this module.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx

from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import EgressBlocked

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_METADATA = ipaddress.ip_network("169.254.0.0/16")
_PRIVATE_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_blocked(ip: IPAddress, policy: EgressPolicy) -> bool:
    if policy.block_metadata and ip in _METADATA:
        return True
    return bool(policy.block_private and any(ip in net for net in _PRIVATE_RANGES))


def resolve_all_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return sorted({str(info[4][0]) for info in infos})


def assert_host_allowed(host: str, policy: EgressPolicy) -> None:
    """Raises `EgressBlocked` if *any* of `host`'s resolved IPs are denied --
    a host resolving to both a public and a metadata/private IP does not
    get a pass to use the public one; that mixed-resolution case is exactly
    the DNS-rebinding shape this guard exists to close."""

    for raw_ip in resolve_all_ips(host):
        ip = ipaddress.ip_address(raw_ip)
        if _is_blocked(ip, policy):
            raise EgressBlocked(
                f"{host!r} resolves to {raw_ip!r}, blocked by egress policy"
            )


async def guarded_get(url: str, policy: EgressPolicy, **kwargs: Any) -> httpx.Response:
    """The basic (non-browser) tier's `GET` -- validates before connecting,
    then delegates to a plain `httpx.AsyncClient`."""

    assert_host_allowed(httpx.URL(url).host, policy)
    async with httpx.AsyncClient() as client:
        return await client.get(url, **kwargs)
