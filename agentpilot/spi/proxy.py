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
    sticky_key: Hashable | None = None
    """Defaults to the owning IdentityKey at assignment time (explicit form of
    the same get-or-create sticky-proxy-pinning pattern used by a prior
    internal system)."""
