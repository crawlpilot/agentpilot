"""Gateway-side background task: detects dead worker nodes (heartbeat TTL
expiry) and cleans up their state -- sessions, leases, affinities -- so a
crashed worker doesn't leave permanent zombies for clients to route into.
Redis-lock-elected so exactly one gateway instance acts even with N replicas,
using `redis.asyncio.Redis.lock()`'s CAS-based acquire/extend/release --
not a hand-rolled `SET NX` repeated every cycle, which cannot re-acquire its
own lock (a plain `NX` fails identically whether you or someone else holds
it) and would silently strand leadership after the first cycle.

Uses `RedisRegistry.evict()`, never `force_release()`, to clear a dead
node's leases -- `force_release` only flips ACTIVE -> IDLE while *preserving*
context_id/node_id (see `force_release.lua`'s own docstring: "Releases to
IDLE, never destroys"), which would tell the next `acquire()` a warm context
still exists on a node that's gone. `evict()` deletes the row outright,
forcing a genuinely fresh `driver.open()` -- "degraded to cold, never wrong"
(plan.md).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import structlog
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError

from agentpilot.observability.metrics import (
    node_reaper_nodes_reaped_total,
    node_reaper_sessions_reclaimed_total,
)
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.identity import IdentityKey

log = structlog.get_logger(__name__)

_LUA_DIR = Path(__file__).resolve().parent.parent / "session" / "lua"


def _load(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class NodeReaper:
    def __init__(
        self,
        redis: Redis,
        registry: RegistryProtocol,
        *,
        scan_interval_seconds: float = 5.0,
        lock_ttl_seconds: float = 15.0,
    ) -> None:
        self._redis = redis
        self._registry = registry
        self._scan_interval_seconds = scan_interval_seconds
        self._clear_stale_affinity = redis.register_script(_load("clear_stale_affinity.lua"))
        self._lock: Lock = redis.lock("node_reaper_lock", timeout=lock_ttl_seconds, blocking=False)
        self._holding_lock = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._holding_lock:
            with contextlib.suppress(LockError):
                await self._lock.release()
            self._holding_lock = False

    async def _run(self) -> None:
        while True:
            try:
                await self._cycle()
            except Exception:
                log.exception("node_reaper.cycle_failed")
            await asyncio.sleep(self._scan_interval_seconds)

    async def _cycle(self) -> None:
        if self._holding_lock:
            try:
                await self._lock.extend(self._scan_interval_seconds * 3, replace_ttl=True)
            except LockError:
                self._holding_lock = False
        if not self._holding_lock:
            self._holding_lock = bool(await self._lock.acquire(blocking=False))
        if not self._holding_lock:
            return  # another gateway instance holds the lock this cycle
        await self.scan_once()

    async def scan_once(self) -> None:
        for raw_node_id in await self._redis.smembers("live_nodes"):
            node_id = _decode(raw_node_id)
            if await self._redis.exists(f"capacity:{node_id}"):
                continue
            await self._reap_node(node_id)

    async def _reap_node(self, node_id: str) -> None:
        session_ids = [_decode(s) for s in await self._redis.smembers(f"node_sessions:{node_id}")]
        for session_id in session_ids:
            raw = await self._redis.hgetall(f"session:{session_id}")
            if raw:
                identity = IdentityKey(
                    tenant=_decode(raw.get(b"tenant", b"")),
                    domain=_decode(raw.get(b"domain", b"")),
                    name=_decode(raw.get(b"name", b"")),
                )
                await self._registry.evict(identity)
                await self._clear_stale_affinity(
                    keys=[f"affinity:{identity.slug()}"], args=[node_id]
                )
                node_reaper_sessions_reclaimed_total.inc()
            await self._redis.delete(f"session:{session_id}")

        await self._redis.delete(
            f"node:{node_id}", f"capacity:{node_id}", f"node_sessions:{node_id}"
        )
        await self._redis.srem("live_nodes", node_id)
        node_reaper_nodes_reaped_total.inc()
        log.warning("node_reaper.node_reaped", node_id=node_id, session_count=len(session_ids))
