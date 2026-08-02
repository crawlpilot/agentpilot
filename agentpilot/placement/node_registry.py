"""Worker-side node self-registration + capacity heartbeat -- the piece that
turns "one hardcoded AGENTPILOT_WORKER_URL" into a real fleet the gateway's
`SessionPlacer`/`NodeReaper` can see. Same background-task shape as
`agentpilot.session.reaper.Reaper`: idempotent start/stop, a `while True`
loop that logs and keeps going on a bad iteration rather than dying.

Two writes, two different lifetimes:
- `node:{node_id}` (no TTL) -- written once at boot via `register()`. Only
  the gateway's node-reaper ever deletes it, once `capacity:{node_id}`'s
  heartbeat has gone stale -- so a worker's own crash (no graceful `stop()`)
  is exactly what the reaper exists to detect and clean up after.
- `capacity:{node_id}` (10s TTL, refreshed every 2s) -- the actual liveness
  signal. `live_nodes` SET membership is never trusted alone as "this node
  is up" (see `place_session.lua`'s docstring) -- only this key's presence is.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import structlog
from redis.asyncio import Redis

from agentpilot.session.reaper import read_meminfo_used_pct
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.lease import ContextState

log = structlog.get_logger(__name__)


def read_cpu_used_pct() -> float | None:
    """Best-effort 1-minute load average, normalized by core count -- same
    "don't pull in psutil for one gauge" reasoning as `read_meminfo_used_pct`.
    Purely advisory (reported in the heartbeat for observability); placement
    admission itself is driven by `active`/`max_contexts` only, not this."""

    try:
        with open("/proc/loadavg") as f:
            load_1m = float(f.read().split()[0])
        cpu_count = os.cpu_count() or 1
        return min(load_1m / cpu_count * 100, 100.0)
    except OSError:
        return None


class NodeRegistry:
    def __init__(
        self,
        redis: Redis,
        registry: RegistryProtocol,
        *,
        node_id: str,
        addr: str,
        max_contexts: int,
        heartbeat_interval_seconds: float = 2.0,
        ttl_seconds: float = 10.0,
    ) -> None:
        self._redis = redis
        self._registry = registry
        self._node_id = node_id
        self._addr = addr
        self._max_contexts = max_contexts
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._ttl_seconds = ttl_seconds
        self._task: asyncio.Task[None] | None = None

    async def register(self) -> None:
        await self._redis.hset(
            f"node:{self._node_id}", mapping={"addr": self._addr, "started_at": time.time()}
        )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Best-effort only -- a crash never reaches this; the gateway's
        # NodeReaper is the real backstop once capacity:{id}'s TTL expires.
        with contextlib.suppress(Exception):
            await self._redis.delete(f"node:{self._node_id}", f"capacity:{self._node_id}")
            await self._redis.srem("live_nodes", self._node_id)

    async def _run(self) -> None:
        while True:
            try:
                await self._heartbeat()
            except Exception:
                log.exception("node_registry.heartbeat_failed", node_id=self._node_id)
            await asyncio.sleep(self._heartbeat_interval_seconds)

    async def _heartbeat(self) -> None:
        active = idle = 0
        for _identity, ctx, _lease, _released_at in await self._registry.snapshot():
            if ctx.node_id != self._node_id:
                continue
            if ctx.state is ContextState.ACTIVE:
                active += 1
            elif ctx.state is ContextState.IDLE:
                idle += 1

        mem_used_pct = read_meminfo_used_pct()
        cpu_used_pct = read_cpu_used_pct()

        async with self._redis.pipeline() as pipe:
            # `node:{id}` (addr, no TTL) is re-asserted on every heartbeat,
            # not just once at boot in `register()`: a node that briefly
            # missed its `capacity:{id}` TTL gets reaped (`NodeReaper`
            # deletes both `capacity:{id}` *and* `node:{id}`), but this loop
            # never stops and unconditionally re-adds `live_nodes` +
            # `capacity:{id}` on the very next tick either way -- without
            # re-asserting `node:{id}` here too, that self-heal was a lie:
            # the node looked live (`live_nodes`/`capacity:{id}` present)
            # while `resolve_node_addr()` kept raising `NodeLost` for it
            # forever, since nothing ever wrote `node:{id}` again short of a
            # full process restart. Observed directly: both dev workers
            # stuck exactly in that state after a single reap, permanently
            # unroutable despite `docker ps` showing them healthy.
            pipe.hset(
                f"node:{self._node_id}", mapping={"addr": self._addr, "started_at": time.time()}
            )
            pipe.hset(
                f"capacity:{self._node_id}",
                mapping={
                    "max_contexts": self._max_contexts,
                    "active": active,
                    "idle": idle,
                    "mem_used_pct": mem_used_pct if mem_used_pct is not None else "",
                    "cpu_used_pct": cpu_used_pct if cpu_used_pct is not None else "",
                },
            )
            pipe.expire(f"capacity:{self._node_id}", int(self._ttl_seconds))
            pipe.sadd("live_nodes", self._node_id)
            await pipe.execute()
