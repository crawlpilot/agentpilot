"""Lookups the *consumer* answers, not the browser layer.

Phase 3b. Two things the browser needs but has no business knowing:

- **which egress endpoints an identity may use.** Today that is resolved from a
  `(tenant, tier)` table -- and a tenant is a customer, a billing and
  segmentation concept that has no place in a browser library (plan D10).
- **which prototype profile to seed a fresh identity from.** Today that is a
  directory tree keyed by domain -- a catalog, and catalogs are the consumer's
  data model, not ours.

Both become Protocols with inert defaults. `browserpilot` defines no catalog and
no schema, only the question it needs answered; the platform answers it from
whatever it actually stores. This is the line drawn in the plan: a Protocol
asking a question is a seam, while a data type plus a store plus shipped
instances is management.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from agentpilot.spi.identity import IdentityRef
from agentpilot.spi.proxy import ProxyEndpoint


@runtime_checkable
class ProxyProvider(Protocol):
    """The candidate egress pool for an identity on a tier, most-specific first.

    Takes the whole identity rather than a tenant string: the browser layer must
    not know that an identity *has* a tenant. A single-tenant crawler ignores
    the argument entirely; the platform reads whatever segmentation it keeps.
    """

    def endpoints_for(
        self, identity: IdentityRef, tier: str | None = None
    ) -> list[ProxyEndpoint]: ...

    def all_endpoints(self) -> list[ProxyEndpoint]: ...

    @property
    def is_empty(self) -> bool: ...


@runtime_checkable
class PrototypeProvider(Protocol):
    """The prototype profile to seed a fresh identity for `origin` from, if any.

    Keyed on origin, never on a site name: a per-origin prototype is what makes
    seeding worth doing at all, because the cookies that matter (`_abck`,
    `datadome`) are origin-scoped, so a profile warmed on one site carries
    nothing useful for another.
    """

    def prototype_for(self, origin: str) -> Path | None: ...


class StaticProxies:
    """A flat, tenant-blind endpoint list -- the shipped default.

    This is what a crawler with one proxy pool (or none) wants, and it is why
    the browser layer needs no `(tenant, tier)` table of its own.
    """

    def __init__(self, endpoints: Sequence[ProxyEndpoint] = ()) -> None:
        self._endpoints = list(endpoints)

    def endpoints_for(
        self, identity: IdentityRef, tier: str | None = None
    ) -> list[ProxyEndpoint]:
        if tier is None:
            return list(self._endpoints)
        # Honour an explicit tier request when the endpoints declare one, but
        # never yield an empty pool for a valid request -- same fall-through the
        # tenant-aware implementation uses.
        matching = [e for e in self._endpoints if e.tier == tier]
        return matching or list(self._endpoints)

    def all_endpoints(self) -> list[ProxyEndpoint]:
        return list(self._endpoints)

    @property
    def is_empty(self) -> bool:
        return not self._endpoints


class NullPrototypes:
    """Seeding disabled -- the shipped default. A caller that wants warmed
    prototypes supplies a provider that knows where they live."""

    def prototype_for(self, origin: str) -> Path | None:
        return None
