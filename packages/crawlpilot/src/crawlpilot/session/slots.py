"""How many browsers this node is holding, counted so that a dead holder stops
counting on its own.

The reaper answers "what should I take back". This answers "may one more be
opened", and the two are not the same question: the reaper can only take what is
IDLE, and under sustained load nothing is, so the watermark sits there correct
and powerless while the node fills up.

**The previous attempt at this counted by scanning, and that is the bug.**
`NodeAdmission` read `len(await registry.snapshot())`, and the Redis snapshot is
`scan_iter(match="active:*")` plus an `HGETALL` per key. Two things follow, both
measured:

    $ grep -n "EXPIRE\\|PEXPIRE\\|SETEX" agentpilot/control/lua/*.lua
    (no matches)
    $ redis-cli SCARD live_nodes                            -> 2
    $ redis-cli --scan --pattern 'node_sessions:*' | wc -l  -> 18

Not one Lua script set an expiry, so every key those scripts wrote outlived the
process that wrote it -- sixteen of those eighteen belonged to containers that no
longer existed, and every rebuild leaked another. A count taken by scanning keys
that nothing removes only ever grows.

And it was not atomic. The count was read in Python, the browser opened after,
and the registry's lock is per-*identity* -- so two different identities
serialize against nothing and both pass the same check. The log recorded the
consequence directly: `admission.refused_max_contexts live=5 max_contexts=4`.

**The shape this borrows.** firecrawl's concurrency limiter
(`apps/api/src/lib/concurrency-redis.ts`) makes a slot a sorted-set member whose
*score is its expiry*:

    zadd(key, now + timeout, id);          // claim or renew -- same call
    zrangebyscore(key, now, Infinity);     // who is live, right now

A holder that dies stops being counted the moment its score passes `now`. There
is no reaper in that path, no heartbeat to miss, and no cleanup to forget --
expiry is intrinsic to the structure, so the failure mode is a slot released
*early*, which self-corrects, rather than a slot leaked forever, which does not.
Renewal being the same operation as claiming is what makes a heartbeat
idempotent and stateless.

**What a slot means here.** Not a lease -- a *browser that exists*. A context
holds memory while IDLE exactly as it does while ACTIVE, so the slot is claimed
when one is opened, refreshed while it lives, and released only when it is
destroyed. The TTL is therefore a backstop measured against the heartbeat, not a
lifetime: anything still being renewed is still running.

The Redis implementation is deliberately NOT here. Counting and claiming have to
happen in the same atomic step as the lease reservation, and that step is
`acquire_lease.lua`; splitting it back into two round trips would reintroduce the
race this exists to close. This module is the in-process table and the shape both
sides implement.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

import structlog

from crawlpilot.spi.errors import NodeAtCapacity

log = structlog.get_logger(__name__)


class SlotTable(Protocol):
    """One node's browser slots.

    `claim` is the only method that can refuse, and it refuses by raising
    `NodeAtCapacity` rather than returning a bool, so a caller cannot forget to
    check it -- the same reasoning `resolve.py` gives for raising rather than
    returning sentinels.
    """

    async def claim(self, slug: str, *, ttl_seconds: float, max_slots: int) -> None: ...

    async def renew(self, slug: str, *, ttl_seconds: float) -> None: ...

    async def release(self, slug: str) -> None: ...

    async def live(self) -> int: ...


class InMemorySlotTable:
    """A slot table for one process, with the same expiry-as-score semantics.

    The deadline map is exactly the sorted set, minus the ordering that Redis
    needs for a range query and Python does not for a dict this small (one node
    holds single-digit browsers; the whole point is that it may not hold many).

    The lock is process-wide, NOT per-identity, and that is the correction. A
    per-identity lock makes two *different* identities concurrent, which is
    precisely the pair that races on a shared budget -- `Registry._lock_for`
    serializes the wrong thing for this question. Sweep, count and claim have to
    be one critical section or the count is a guess by the time it is used.
    """

    def __init__(self) -> None:
        self._deadlines: dict[str, float] = {}
        self._guard = asyncio.Lock()

    async def claim(self, slug: str, *, ttl_seconds: float, max_slots: int) -> None:
        async with self._guard:
            now = time.monotonic()
            self._sweep(now)
            # Already holding one means this is a renewal, and a renewal is
            # never refused: the browser exists either way, so declining costs
            # the caller its run and frees nothing. This is the invariant that
            # keeps warm reuse working under pressure -- refusing a reuse would
            # stop the node doing the work that releases slots.
            if slug not in self._deadlines and len(self._deadlines) >= max_slots:
                log.warning(
                    "slots.refused", slug=slug, live=len(self._deadlines), max_slots=max_slots
                )
                raise NodeAtCapacity(
                    f"this node already holds {len(self._deadlines)} browser contexts "
                    f"(max {max_slots}); retry shortly"
                )
            self._deadlines[slug] = now + ttl_seconds

    async def renew(self, slug: str, *, ttl_seconds: float) -> None:
        """Refresh a deadline without consulting the budget.

        Separate from `claim` only so a caller states which it means. Renewing
        through `claim` would give the same answer -- the "already holding"
        branch above -- but it would read as though a heartbeat could be
        refused, and it cannot.
        """

        async with self._guard:
            self._deadlines[slug] = time.monotonic() + ttl_seconds

    async def release(self, slug: str) -> None:
        async with self._guard:
            self._deadlines.pop(slug, None)

    async def live(self) -> int:
        async with self._guard:
            self._sweep(time.monotonic())
            return len(self._deadlines)

    def _sweep(self, now: float) -> None:
        """Drop what has expired.

        Housekeeping, not correctness: every read that matters filters by
        deadline anyway, so a sweep that never ran would still give the right
        answer. It exists to stop a long-lived process accumulating the names of
        browsers that died, which is the failure this whole module is a response
        to.
        """

        dead = [slug for slug, deadline in self._deadlines.items() if deadline <= now]
        for slug in dead:
            del self._deadlines[slug]
        if dead:
            log.info("slots.expired", slugs=dead)
