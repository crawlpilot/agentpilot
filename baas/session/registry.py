"""In-memory session registry -- the real version of P0's `wiring.py`
`active_identities`/`warm_contexts` dicts (a port of Browser4's
`ConcurrentStatefulPrivacyContextPool.computeIfAbsent()`), now with a
per-`IdentityKey` `asyncio.Lock` instead of one global lock. P2 swaps the
in-memory dicts for Redis + Lua behind this same interface -- the
<=1-ACTIVE-per-identity invariant enforced here is exactly what
`bind_active_context.lua` re-implements atomically, not a different rule.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from baas.session.lease import new_lease
from baas.session.lease import renew as _renew_lease
from baas.spi.errors import LeaseConflict
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, ContextState, Lease, LeaseId

Opener = Callable[[], Awaitable[ContextRef]]


@runtime_checkable
class RegistryProtocol(Protocol):
    """The shared shape `Registry` (in-memory, this file) and
    `session.redis_registry.RedisRegistry` (P2) both implement -- `Reaper`
    and the gateway/worker routes depend on this Protocol, never on the
    concrete in-memory class, so swapping backends is a one-line wiring
    change, not a rewrite of everything that uses a registry."""

    async def acquire(
        self, identity: IdentityKey, owner: str, ttl_seconds: float, opener: Opener
    ) -> tuple[ContextRef, Lease]: ...

    async def renew(self, lease_id: LeaseId) -> Lease: ...

    async def release(self, lease_id: LeaseId) -> None: ...

    async def snapshot(
        self,
    ) -> list[tuple[IdentityKey, ContextRef, Lease | None, float | None]]: ...

    async def evict(self, identity: IdentityKey) -> ContextRef | None: ...

    async def force_release(self, identity: IdentityKey) -> None: ...


@dataclass
class _Entry:
    context_ref: ContextRef
    lease: Lease | None = None
    released_at: float | None = None
    """Monotonic timestamp of the last release-to-IDLE; `None` while ACTIVE.
    The reaper's idle-TTL scan and memory-pressure LRU eviction both key off
    this rather than re-deriving "how long idle" from wall-clock lease data."""


class Registry:
    def __init__(self) -> None:
        self._entries: dict[IdentityKey, _Entry] = {}
        self._lease_owner: dict[LeaseId, IdentityKey] = {}
        self._identity_locks: dict[IdentityKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, identity: IdentityKey) -> asyncio.Lock:
        async with self._locks_guard:
            return self._identity_locks.setdefault(identity, asyncio.Lock())

    async def acquire(
        self, identity: IdentityKey, owner: str, ttl_seconds: float, opener: Opener
    ) -> tuple[ContextRef, Lease]:
        """`computeIfAbsent` for a warm context: reuses an IDLE entry for
        `identity` if one exists (a still-running Chrome from a prior
        release), opens a fresh one via `opener` otherwise. Raises
        `LeaseConflict` if `identity` already holds an ACTIVE lease -- the
        whole critical section runs under this identity's own lock, so two
        concurrent opens for the same identity can't both win the race.
        """

        lock = await self._lock_for(identity)
        async with lock:
            entry = self._entries.get(identity)
            if entry is not None and entry.context_ref.state is ContextState.ACTIVE:
                raise LeaseConflict(f"identity {identity.slug()!r} already has an active session")

            if entry is None:
                context_ref = await opener()
                entry = _Entry(context_ref=context_ref)
                self._entries[identity] = entry
            else:
                entry.context_ref.state = ContextState.ACTIVE
                entry.released_at = None

            lease = new_lease(identity, owner, ttl_seconds, entry.context_ref)
            entry.lease = lease
            self._lease_owner[lease.lease_id] = identity
            return entry.context_ref, lease

    async def renew(self, lease_id: LeaseId) -> Lease:
        identity = self._lease_owner.get(lease_id)
        if identity is None:
            raise KeyError(f"no such lease {lease_id!r}")
        lock = await self._lock_for(identity)
        async with lock:
            entry = self._entries.get(identity)
            if entry is None or entry.lease is None or entry.lease.lease_id != lease_id:
                # The reaper reclaimed this lease (idle-TTL, memory pressure,
                # or lease-expiry) between the caller's last renewal and now.
                self._lease_owner.pop(lease_id, None)
                raise KeyError(f"lease {lease_id!r} was reclaimed")
            entry.lease = _renew_lease(entry.lease)
            return entry.lease

    async def release(self, lease_id: LeaseId) -> None:
        identity = self._lease_owner.pop(lease_id, None)
        if identity is None:
            return
        lock = await self._lock_for(identity)
        async with lock:
            entry = self._entries.get(identity)
            if entry is None or entry.lease is None or entry.lease.lease_id != lease_id:
                return  # already reclaimed/reacquired under us; nothing to do
            entry.context_ref.state = ContextState.IDLE
            entry.lease = None
            entry.released_at = time.monotonic()

    async def snapshot(self) -> list[tuple[IdentityKey, ContextRef, Lease | None, float | None]]:
        """Read-only view for the reaper/metrics. Safe without a lock: callers
        only read `ContextRef`/`Lease` (never mutate), and the identity-level
        locks only ever protect registry bookkeeping, not these reads.
        `async def` only to match `RegistryProtocol` (the Redis backend's
        equivalent is real I/O); this implementation has nothing to await."""

        return [
            (identity, e.context_ref, e.lease, e.released_at)
            for identity, e in self._entries.items()
        ]

    async def evict(self, identity: IdentityKey) -> ContextRef | None:
        """Removes and returns the entry so the reaper can destroy it.
        Distinct from `release()` (ACTIVE -> IDLE, context stays warm):
        eviction destroys the underlying context entirely."""

        lock = await self._lock_for(identity)
        async with lock:
            entry = self._entries.pop(identity, None)
            if entry is None:
                return None
            if entry.lease is not None:
                self._lease_owner.pop(entry.lease.lease_id, None)
            return entry.context_ref

    async def force_release(self, identity: IdentityKey) -> None:
        """Reaper-only: reclaims an ACTIVE lease whose owner let it expire
        without renewing (a crashed/abandoned client) -- releases to IDLE
        rather than destroying, so the identity is still warm for a fresh
        open(). Ordinary release() is keyed by lease_id (the caller proves
        ownership); this is keyed by identity because the reaper is acting
        *because* the owner is presumed gone."""

        lock = await self._lock_for(identity)
        async with lock:
            entry = self._entries.get(identity)
            if entry is None or entry.lease is None:
                return
            self._lease_owner.pop(entry.lease.lease_id, None)
            entry.context_ref.state = ContextState.IDLE
            entry.lease = None
            entry.released_at = time.monotonic()
