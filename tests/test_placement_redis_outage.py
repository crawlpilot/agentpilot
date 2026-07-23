"""Confirms the gateway's placement layer fails closed (propagates a real
error) rather than silently defaulting to some worker when Redis is
unreachable -- plan.md's explicit requirement for the fleet placement
design."""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from agentpilot.placement.placer import SessionPlacer
from agentpilot.spi.identity import IdentityKey

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="a")


class _UnreachableRedis:
    """Minimal stand-in for `redis.asyncio.Redis` whose registered scripts
    always raise, simulating a Redis outage at call time."""

    def register_script(self, _script: str):
        async def _raise(*_args: object, **_kwargs: object) -> None:
            raise RedisConnectionError("Redis is unreachable")

        return _raise


async def test_placement_fails_closed_when_redis_unreachable() -> None:
    placer = SessionPlacer(_UnreachableRedis())
    with pytest.raises(RedisConnectionError):
        await placer.place(IDENTITY, affinity_ttl_seconds=60)
