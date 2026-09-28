"""In-memory session registry -- the real version of P0's `wiring.py`
`active_identities`/`warm_contexts` dicts (a port of a prior internal
system's get-or-create-under-lock context pool), now with a
per-`IdentityRef` `asyncio.Lock` instead of one global lock. P2 swaps the
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

from crawlpilot.session.admission import Admit
from crawlpilot.session.lease import new_lease
from crawlpilot.session.lease import renew as _renew_lease
from crawlpilot.session.slots import SlotTable
from crawlpilot.spi.errors import LeaseConflict
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState, Lease, LeaseId

Opener = Callable[[], Awaitable[ContextRef]]


@runtime_checkable
class RegistryProtocol(Protocol):
    """The shared shape `Registry` (in-memory, this file) and
    `session.redis_registry.RedisRegistry` (P2) both implement -- `Reaper`
    and the gateway/worker routes depend on this Protocol, never on the
    concrete in-memory class, so swapping backends is a one-line wiring
    change, not a rewrite of everything that uses a registry."""

    async def acquire(
        self, identity: IdentityRef, owner: str, ttl_seconds: float, opener: Opener
    ) -> tuple[ContextRef, Lease]: ...

    async def renew(self, lease_id: LeaseId) -> Lease: ...

    async def release(self, lease_id: LeaseId) -> None: ...

    async def snapshot(
        self,
    ) -> list[tuple[IdentityRef, ContextRef, Lease | None, float | None]]: ...

    async def evict(self, identity: IdentityRef) -> ContextRef | None: ...

    async def force_release(self, identity: IdentityRef) -> None: ...


@dataclass
class _Entry:
    context_ref: ContextRef
    lease: Lease | None = None
    released_at: float | None = None
    """Monotonic timestamp of the last release-to-IDLE; `None` while ACTIVE.
    The reaper's idle-TTL scan and memory-pressure LRU eviction both key off
    this rather than re-deriving "how long idle" from wall-clock lease data."""


class Registry:
    def __init__(
        self,
        *,
        admit: Admit | None = None,
        slots: SlotTable | None = None,
        slot_ttl_seconds: float = 900.0,
        max_contexts: int = 25,
    ) -> None:
        self._admit = admit
        """Asked before a NEW browser is launched, never before a warm one is
        reused -- reuse costs no memory, so refusing it would be pure loss. See
        `crawlpilot.session.admission`."""
        self._slots = slots
        """The same table `admit` claims into, held here so the slot's deadline
        tracks the browser rather than the request that opened it. A context
        outlives the lease that created it -- it stays warm through IDLE -- so
        the slot is refreshed on renewal and surrendered only at destroy. Making
        it expire with the lease would free the budget while Chrome was still
        resident, which is the accounting error in the other direction."""
        self._slot_ttl_seconds = slot_ttl_seconds
        """How long a slot survives with nothing refreshing it.

        A backstop, not a lifetime. The heartbeat renews every `stale_after/3`
        (~40s by default), so anything still running refreshes this many times
        over; the only thing that reaches the deadline is a holder that stopped
        existing. Sized well above the reaper's idle TTL so an ordinary warm
        context is never un-counted while it is still resident."""
        self._max_contexts = max_contexts
        """How many browsers this node may hold at once.

        Enforced by the slot table rather than by a caller comparing a count it
        read a moment ago -- the check and the claim are one critical section,
        which is the whole correction. See `slots.py`."""
        self._entries: dict[IdentityRef, _Entry] = {}
        self._lease_owner: dict[LeaseId, IdentityRef] = {}
        self._identity_locks: dict[IdentityRef, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, identity: IdentityRef) -> asyncio.Lock:
        async with self._locks_guard:
            return self._identity_locks.setdefault(identity, asyncio.Lock())

    async def acquire(
        self, identity: IdentityRef, owner: str, ttl_seconds: float, opener: Opener
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
            # Owned is owned, whichever way the entry says so.
            #
            # This used to test the state flag alone, and the flag and the lease
            # are two records of the same fact -- so when they disagreed, an
            # identity someone was still using looked free. The next `acquire`
            # then reused the entry, overwrote `entry.lease`, and left the
            # previous holder's id dangling in `_lease_owner`: their next
            # `renew` found a different lease on the entry and raised "lease
            # was reclaimed".
            #
            # That message is what a reaper eviction looks like, so the failure
            # read as a timeout and could not be told apart from one -- no
            # reaper line in the log, because no reaper was involved. Measured
            # on two concurrent recipe builds against the same domain: the
            # second stole the first's warm slot mid-run, and the first died
            # five steps later with every observation failing at once.
            #
            # `release` clears the lease, so a genuinely free entry is still
            # reusable and the warm pool keeps working.
            if entry is not None and (
                entry.context_ref.state is ContextState.ACTIVE or entry.lease is not None
            ):
                raise LeaseConflict(f"identity {identity.slug()!r} already has an active session")

            if entry is None:
                # Nothing here is warm, so this is a new browser. That is the
                # one moment worth refusing at: the reaper can only take back
                # what is IDLE, and under load nothing is.
                if self._admit is not None:
                    await self._admit(identity.slug())
                if self._slots is not None:
                    # Counted and claimed together. The identity lock above
                    # cannot do this job: it serializes one identity against
                    # itself, and the callers that race for the last slot are
                    # by definition *different* identities.
                    await self._slots.claim(
                        identity.slug(),
                        ttl_seconds=self._slot_ttl_seconds,
                        max_slots=self._max_contexts,
                    )
                try:
                    context_ref = await opener()
                except BaseException:
                    # The slot was claimed a line ago and no browser came of it.
                    # Leaving it claimed would bill the node for a context that
                    # does not exist until the deadline passed -- correct
                    # eventually, wrong for as long as the TTL, and the TTL is
                    # sized as a crash backstop rather than a retry interval.
                    if self._slots is not None:
                        await self._slots.release(identity.slug())
                    raise
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
        # Outside the identity lock: the slot table has its own, and nesting two
        # locks in one order here and the other order anywhere else is how a
        # deadlock gets built.
        if self._slots is not None:
            await self._slots.renew(identity.slug(), ttl_seconds=self._slot_ttl_seconds)
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

    async def snapshot(self) -> list[tuple[IdentityRef, ContextRef, Lease | None, float | None]]:
        """Read-only view for the reaper/metrics. Safe without a lock: callers
        only read `ContextRef`/`Lease` (never mutate), and the identity-level
        locks only ever protect registry bookkeeping, not these reads.
        `async def` only to match `RegistryProtocol` (the Redis backend's
        equivalent is real I/O); this implementation has nothing to await."""

        return [
            (identity, e.context_ref, e.lease, e.released_at)
            for identity, e in self._entries.items()
        ]

    async def evict(self, identity: IdentityRef) -> ContextRef | None:
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
        # Destroy is the only thing that frees a slot. `release` moves ACTIVE ->
        # IDLE and the browser is still running and still resident, so the node
        # is still carrying it.
        if self._slots is not None:
            await self._slots.release(identity.slug())
        return entry.context_ref

    async def force_release(self, identity: IdentityRef) -> None:
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
