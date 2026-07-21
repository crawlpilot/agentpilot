"""Driver-agnostic proxy endpoint types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    vendor: str | None = None
    sticky_key: Hashable | None = None
    """Defaults to the owning IdentityKey at assignment time (explicit form of
    Browser4's `activeProxyEntries.computeIfAbsent` pinning)."""
