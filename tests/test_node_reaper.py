"""Unit tests for `agentpilot.placement.node_reaper.NodeReaper` -- against
`fakeredis` (with `lupa` for real Lua-script execution, including the
built-in `redis.asyncio.Redis.lock()` CAS scripts), same pattern as
`test_redis_registry.py`. No live Redis, no Docker."""

from __future__ import annotations

import fakeredis
import pytest

from agentpilot.placement.node_reaper import NodeReaper
from agentpilot.session.redis_registry import RedisRegistry
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="a")


def _make_ctx(node_id: str) -> ContextRef:
    return ContextRef(
        context_id="ctx-1", identity=IDENTITY, state=ContextState.ACTIVE, pid=123, node_id=node_id
    )


async def _opener(node_id: str) -> ContextRef:
    return _make_ctx(node_id)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def registry(redis: fakeredis.aioredis.FakeRedis) -> RedisRegistry:
    return RedisRegistry(redis)


@pytest.fixture
def reaper(redis: fakeredis.aioredis.FakeRedis, registry: RedisRegistry) -> NodeReaper:
    return NodeReaper(redis, registry)


async def _seed_dead_node_session(redis: fakeredis.aioredis.FakeRedis, node_id: str) -> None:
    await redis.sadd("live_nodes", node_id)
    await redis.hset(f"node:{node_id}", mapping={"addr": f"http://{node_id}:8000"})
    # Deliberately no capacity:{node_id} -- heartbeat has expired.
    await redis.hset(
        "session:sess-1",
        mapping={"node_id": node_id, "tenant": "t", "domain": "example.com", "name": "a"},
    )
    await redis.sadd(f"node_sessions:{node_id}", "sess-1")


async def test_scan_once_ignores_live_node_with_capacity_heartbeat(redis, reaper) -> None:
    await redis.hset("capacity:node-a", mapping={"active": 0, "max_contexts": 10})
    await redis.sadd("live_nodes", "node-a")
    await redis.hset("node:node-a", mapping={"addr": "http://node-a:8000"})

    await reaper.scan_once()

    assert await redis.exists("node:node-a")
    assert await redis.sismember("live_nodes", "node-a")


async def test_scan_once_reaps_dead_node_sessions_and_affinity(redis, reaper) -> None:
    await _seed_dead_node_session(redis, "dead-node")
    await redis.set(f"affinity:{IDENTITY.slug()}", "dead-node")

    await reaper.scan_once()

    assert not await redis.exists("session:sess-1")
    assert not await redis.exists("node:dead-node")
    assert not await redis.exists("node_sessions:dead-node")
    assert not await redis.sismember("live_nodes", "dead-node")
    assert await redis.get(f"affinity:{IDENTITY.slug()}") is None


async def test_reaping_uses_evict_not_force_release_no_false_reuse(redis, registry, reaper) -> None:
    """The bug a validation pass caught: force_release would preserve
    context_id, telling a future acquire() a warm context exists on a node
    that's gone. evict() must remove the row outright, so the next acquire()
    genuinely calls opener() again rather than reporting a false reuse."""

    await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener("dead-node"))
    await _seed_dead_node_session(redis, "dead-node")

    await reaper.scan_once()

    calls: list[int] = []

    async def counting_opener() -> ContextRef:
        calls.append(len(calls))
        return _make_ctx("fresh-node")

    ctx, _lease = await registry.acquire(IDENTITY, "owner", 300.0, counting_opener)
    assert len(calls) == 1  # opener WAS called -- no false "reuse" of a dead context
    assert ctx.node_id == "fresh-node"


async def test_fresh_affinity_written_after_node_death_survives_reap(redis, reaper) -> None:
    """A healthy re-placement may write a *new* affinity for the same
    identity between the dead node's last heartbeat and the reaper's scan --
    the reaper must not clobber it (clear_stale_affinity's compare-and-delete
    only clears an affinity that still points at the dead node)."""

    await _seed_dead_node_session(redis, "dead-node")
    # A fresh placement already relocated this identity to a healthy node
    # and wrote its own affinity entry -- before the reaper got to scan.
    await redis.set(f"affinity:{IDENTITY.slug()}", "healthy-node")

    await reaper.scan_once()

    assert await redis.get(f"affinity:{IDENTITY.slug()}") == b"healthy-node"


async def test_lock_election_only_one_of_two_reapers_acts_per_cycle(redis, registry) -> None:
    reaper_a = NodeReaper(redis, registry)
    reaper_b = NodeReaper(redis, registry)
    await _seed_dead_node_session(redis, "dead-node")

    await reaper_a._cycle()
    await reaper_b._cycle()

    assert reaper_a._holding_lock is True
    assert reaper_b._holding_lock is False
    # Only the lock holder (reaper_a) actually reaped.
    assert not await redis.exists("node:dead-node")
