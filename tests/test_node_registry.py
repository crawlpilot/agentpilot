"""Unit tests for `agentpilot.placement.node_registry.NodeRegistry` --
against `fakeredis`, same pattern as `test_redis_registry.py`."""

from __future__ import annotations

import fakeredis
import pytest

from agentpilot.placement.node_registry import NodeRegistry
from agentpilot.session.redis_registry import RedisRegistry
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="a")
OTHER_IDENTITY = IdentityKey(tenant="t", domain="example.com", name="b")


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def registry(redis: fakeredis.aioredis.FakeRedis) -> RedisRegistry:
    return RedisRegistry(redis)


@pytest.fixture
def node_registry(redis: fakeredis.aioredis.FakeRedis, registry: RedisRegistry) -> NodeRegistry:
    return NodeRegistry(
        redis, registry, node_id="node-a", addr="http://node-a:8000", max_contexts=10
    )


async def test_register_writes_node_hash(redis, node_registry) -> None:
    await node_registry.register()
    raw = await redis.hgetall("node:node-a")
    assert raw[b"addr"] == b"http://node-a:8000"
    assert b"started_at" in raw


async def test_heartbeat_writes_capacity_with_ttl_and_live_nodes_membership(
    redis, node_registry
) -> None:
    await node_registry._heartbeat()

    raw = await redis.hgetall("capacity:node-a")
    assert int(raw[b"max_contexts"]) == 10
    assert int(raw[b"active"]) == 0
    assert int(raw[b"idle"]) == 0
    ttl = await redis.ttl("capacity:node-a")
    assert 0 < ttl <= 10
    assert await redis.sismember("live_nodes", "node-a")


async def test_heartbeat_counts_only_this_nodes_contexts(redis, registry, node_registry) -> None:
    async def opener_a() -> ContextRef:
        return ContextRef(
            context_id="ctx-a",
            identity=IDENTITY,
            state=ContextState.ACTIVE,
            pid=1,
            node_id="node-a",
        )

    async def opener_other() -> ContextRef:
        return ContextRef(
            context_id="ctx-b",
            identity=OTHER_IDENTITY,
            state=ContextState.ACTIVE,
            pid=2,
            node_id="node-b",
        )

    await registry.acquire(IDENTITY, "owner", 300.0, opener_a)
    await registry.acquire(OTHER_IDENTITY, "owner", 300.0, opener_other)

    await node_registry._heartbeat()

    raw = await redis.hgetall("capacity:node-a")
    assert int(raw[b"active"]) == 1  # only node-a's own context, not node-b's


async def test_stop_removes_registration(redis, node_registry) -> None:
    await node_registry.register()
    await node_registry._heartbeat()

    await node_registry.stop()

    assert not await redis.exists("node:node-a")
    assert not await redis.exists("capacity:node-a")
    assert not await redis.sismember("live_nodes", "node-a")
