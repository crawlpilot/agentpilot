"""Unit tests for `agentpilot.gateway.routing.resolve_route` -- against
`fakeredis`, no HTTP layer, same unit-level convention as `test_sessions_list.
py`/`test_redis_registry.py` (calls the function directly against a fake
`Wiring` stand-in instead of standing up a `TestClient`)."""

from __future__ import annotations

import fakeredis
import pytest
from fastapi import HTTPException

from agentpilot.gateway.routing import resolve_route
from agentpilot.spi.errors import NodeLost


class _FakeWiring:
    def __init__(self, redis: fakeredis.aioredis.FakeRedis) -> None:
        self.redis = redis


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def wiring(redis: fakeredis.aioredis.FakeRedis) -> _FakeWiring:
    return _FakeWiring(redis)


async def test_resolve_route_404_when_session_missing(wiring) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await resolve_route(wiring, "no-such-session", "tenant-a")
    assert exc_info.value.status_code == 404


async def test_resolve_route_rejects_tenant_mismatch(redis, wiring) -> None:
    await redis.hset("session:sess-1", mapping={"node_id": "node-a", "tenant": "tenant-a"})
    await redis.hset("node:node-a", mapping={"addr": "http://node-a:8000"})

    with pytest.raises(HTTPException) as exc_info:
        await resolve_route(wiring, "sess-1", "tenant-b")
    assert exc_info.value.status_code == 403


async def test_resolve_route_returns_node_id_and_addr_for_owning_tenant(redis, wiring) -> None:
    await redis.hset("session:sess-1", mapping={"node_id": "node-a", "tenant": "tenant-a"})
    await redis.hset("node:node-a", mapping={"addr": "http://node-a:8000"})

    node_id, addr = await resolve_route(wiring, "sess-1", "tenant-a")
    assert node_id == "node-a"
    assert addr == "http://node-a:8000"


async def test_resolve_route_node_lost_when_node_addr_gone(redis, wiring) -> None:
    await redis.hset("session:sess-1", mapping={"node_id": "dead-node", "tenant": "tenant-a"})
    # No node:dead-node -- the node-reaper already cleaned it up.

    with pytest.raises(NodeLost):
        await resolve_route(wiring, "sess-1", "tenant-a")
