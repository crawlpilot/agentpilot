"""Unit tests for `agentpilot.placement.placer.SessionPlacer` /
`place_session.lua` -- against `fakeredis` (with `lupa` for real Lua-script
execution), same pattern as `test_redis_registry.py`. No live Redis, no
Docker."""

from __future__ import annotations

import fakeredis
import pytest

from agentpilot.placement.placer import SessionPlacer
from agentpilot.spi.errors import CapacityExhausted, LeaseConflict
from agentpilot.spi.identity import IdentityKey

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="a")


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def placer(redis: fakeredis.aioredis.FakeRedis) -> SessionPlacer:
    return SessionPlacer(redis)


async def _register_live_node(
    redis: fakeredis.aioredis.FakeRedis, node_id: str, *, active: int, max_contexts: int
) -> None:
    await redis.hset(
        f"capacity:{node_id}", mapping={"active": active, "max_contexts": max_contexts}
    )
    await redis.sadd("live_nodes", node_id)


async def test_no_affinity_places_on_least_loaded_node(redis, placer) -> None:
    await _register_live_node(redis, "node-a", active=5, max_contexts=10)
    await _register_live_node(redis, "node-b", active=1, max_contexts=10)

    node_id = await placer.place(IDENTITY, affinity_ttl_seconds=60)
    assert node_id == "node-b"


async def test_least_loaded_uses_ratio_not_raw_count(redis, placer) -> None:
    # node-a: 8/100 = 0.08 active ratio; node-b: 1/10 = 0.1 -- node-a has a
    # *higher* raw active count but a *lower* ratio, and should still win.
    await _register_live_node(redis, "node-a", active=8, max_contexts=100)
    await _register_live_node(redis, "node-b", active=1, max_contexts=10)

    node_id = await placer.place(IDENTITY, affinity_ttl_seconds=60)
    assert node_id == "node-a"


async def test_affinity_hit_places_on_affinity_node(redis, placer) -> None:
    await _register_live_node(redis, "node-a", active=0, max_contexts=10)
    await _register_live_node(redis, "node-b", active=0, max_contexts=10)
    await redis.set(f"affinity:{IDENTITY.slug()}", "node-b")

    node_id = await placer.place(IDENTITY, affinity_ttl_seconds=60)
    assert node_id == "node-b"


async def test_affinity_full_and_identity_not_active_elsewhere_relocates(redis, placer) -> None:
    await _register_live_node(redis, "node-a", active=10, max_contexts=10)  # full
    await _register_live_node(redis, "node-b", active=0, max_contexts=10)
    await redis.set(f"affinity:{IDENTITY.slug()}", "node-a")
    # No active:{slug} hash at all -- identity isn't ACTIVE anywhere, so
    # relocating away from the full affinity target is safe.

    node_id = await placer.place(IDENTITY, affinity_ttl_seconds=60)
    assert node_id == "node-b"


async def test_affinity_full_and_identity_active_elsewhere_raises_lease_conflict(
    redis, placer
) -> None:
    await _register_live_node(redis, "node-a", active=10, max_contexts=10)  # full
    await redis.set(f"affinity:{IDENTITY.slug()}", "node-a")
    await redis.hset(f"active:{IDENTITY.slug()}", mapping={"state": "active"})

    with pytest.raises(LeaseConflict):
        await placer.place(IDENTITY, affinity_ttl_seconds=60)


async def test_no_live_node_raises_capacity_exhausted(redis, placer) -> None:
    with pytest.raises(CapacityExhausted):
        await placer.place(IDENTITY, affinity_ttl_seconds=60)


async def test_live_nodes_member_without_capacity_heartbeat_is_ignored(redis, placer) -> None:
    # A node listed in live_nodes but whose capacity:{id} has already
    # expired -- membership alone must never be trusted as liveness.
    await redis.sadd("live_nodes", "ghost-node")

    with pytest.raises(CapacityExhausted):
        await placer.place(IDENTITY, affinity_ttl_seconds=60)


async def test_place_increments_active_and_release_reservation_decrements(redis, placer) -> None:
    await _register_live_node(redis, "node-a", active=0, max_contexts=10)

    node_id = await placer.place(IDENTITY, affinity_ttl_seconds=60)
    assert node_id == "node-a"
    assert int(await redis.hget("capacity:node-a", "active")) == 1

    await placer.release_reservation(node_id)
    assert int(await redis.hget("capacity:node-a", "active")) == 0


async def test_commit_route_writes_session_hash_and_node_sessions_set(redis, placer) -> None:
    await placer.commit_route("sess-1", "node-a", IDENTITY, "auto", ttl_seconds=300)

    raw = await redis.hgetall("session:sess-1")
    assert raw[b"node_id"] == b"node-a"
    assert raw[b"tenant"] == b"t"
    assert raw[b"domain"] == b"example.com"
    assert raw[b"name"] == b"a"
    assert raw[b"tier"] == b"auto"
    ttl = await redis.ttl("session:sess-1")
    assert 0 < ttl <= 300
    assert await redis.sismember("node_sessions:node-a", "sess-1")
