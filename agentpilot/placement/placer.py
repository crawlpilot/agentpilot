"""Gateway-side session placement: chooses which live worker a NEW session
lands on. Two-phase, mirroring `RedisRegistry.acquire()`'s own two-phase
shape for the identical reason -- `place_session.lua` can only *reserve* a
node (Lua can't await the multi-second worker HTTP call that follows); the
actual `session:{id}` route is committed by `commit_route()` afterward, once
the worker's response carries back the `session_id` it minted (the gateway
never mints session ids -- see `gateway/routes/sessions.py`'s `open_session`).
"""

from __future__ import annotations

import time
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agentpilot.observability.metrics import placement_decisions_total
from agentpilot.spi.errors import CapacityExhausted, LeaseConflict
from agentpilot.spi.identity import IdentityKey

_LUA_DIR = Path(__file__).resolve().parent.parent / "session" / "lua"


def _load(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class SessionPlacer:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._place = redis.register_script(_load("place_session.lua"))

    async def place(self, identity: IdentityKey, affinity_ttl_seconds: float) -> str:
        """Returns the chosen node_id. Raises `LeaseConflict` (409 -- the
        identity is ACTIVE on a full/dead affinity target, relocating would
        orphan a live context) or `CapacityExhausted` (503 -- no live node
        has room)."""

        try:
            node_id, outcome = await self._place(
                keys=["live_nodes", f"active:{identity.slug()}"],
                args=[identity.slug(), affinity_ttl_seconds],
            )
        except ResponseError as exc:
            if "IDENTITY_ACTIVE_ELSEWHERE" in str(exc):
                raise LeaseConflict(
                    f"identity {identity.slug()!r} already has an active session elsewhere"
                ) from exc
            if "NO_CAPACITY" in str(exc):
                placement_decisions_total.labels(outcome="no_capacity").inc()
                raise CapacityExhausted("no worker node has capacity") from exc
            raise
        placement_decisions_total.labels(outcome=_decode(outcome)).inc()
        return _decode(node_id)

    async def release_reservation(self, node_id: str) -> None:
        """Best-effort undo of `place()`'s optimistic capacity increment --
        called when the worker HTTP call that should follow a successful
        placement fails. Self-heals via the node's own next heartbeat
        regardless, so a failure here is never worth surfacing."""

        try:
            await self._redis.hincrby(f"capacity:{node_id}", "active", -1)
        except Exception:
            pass

    async def commit_route(
        self,
        session_id: str,
        node_id: str,
        identity: IdentityKey,
        tier: str,
        ttl_seconds: float,
    ) -> None:
        """Writes the actual `session:{id}` route now that the worker has
        responded with a real session_id -- no Lua needed, a freshly minted
        id has no concurrent writer to race."""

        async with self._redis.pipeline() as pipe:
            pipe.hset(
                f"session:{session_id}",
                mapping={
                    "node_id": node_id,
                    "tenant": identity.tenant,
                    "domain": identity.domain,
                    "name": identity.name,
                    "tier": tier,
                    "state": "active",
                    "created_at": time.time(),
                },
            )
            pipe.expire(f"session:{session_id}", int(ttl_seconds))
            pipe.sadd(f"node_sessions:{node_id}", session_id)
            await pipe.execute()
