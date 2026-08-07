"""Driver-agnostic proxy endpoint types."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    vendor: str | None = None
    tier: str | None = None
    """Proxy quality class -- `residential` / `mobile` / `datacenter`. The
    scrape tier router asks for `residential` on the stealth/enhanced rungs
    (datacenter IPs are the dominant Akamai edge-block signal); `None` means
    an untiered/default pool entry."""
    country: str | None = None
    """ISO-3166 alpha-2 country of the exit IP, when the provider/config
    declares it. Used to keep the pinned fingerprint's timezone/locale
    consistent with the egress geo (`identity.fingerprint.generate(region=...)`)."""
    sticky_key: Hashable | None = None
    """Defaults to the owning IdentityKey at assignment time (explicit form of
    the same get-or-create sticky-proxy-pinning pattern used by a prior
    internal system)."""
