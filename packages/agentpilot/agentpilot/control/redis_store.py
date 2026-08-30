"""Redis-backed `StateStore` -- the shared, cross-process implementation.

A deliberately thin pass-through: the Redis calls here are exactly the ones
`identity.burn_tracker`, `identity.proxy_health` and `identity.proxy_pinning`
made inline before Phase 3, so their semantics (INCRBY/EXPIRE counters,
HSETNX-once caps) cannot drift from what the browser layer expects.
"""

from __future__ import annotations

from redis.asyncio import Redis


def _text(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


class RedisStateStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def incr_by(self, key: str, delta: int) -> int:
        return int(await self._redis.incrby(key, delta))

    async def get_int(self, key: str) -> int | None:
        raw = _text(await self._redis.get(key))
        return int(raw) if raw is not None else None

    async def set_int(self, key: str, value: int) -> None:
        await self._redis.set(key, value)

    async def expire(self, key: str, ttl_seconds: int) -> None:
        await self._redis.expire(key, ttl_seconds)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.delete(*keys)

    async def hget(self, key: str, field: str) -> str | None:
        return _text(await self._redis.hget(key, field))

    async def hset(self, key: str, field: str, value: str) -> None:
        await self._redis.hset(key, field, value)

    async def hsetnx(self, key: str, field: str, value: str) -> bool:
        return bool(await self._redis.hsetnx(key, field, value))

    async def hincr_by(self, key: str, field: str, delta: int) -> int:
        return int(await self._redis.hincrby(key, field, delta))
