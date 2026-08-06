"""Assign-once-keep-for-life proxy pinning -- a prior internal system's
proxy-pool-pinning pattern, backed by Redis `HSETNX` so the pin
survives process restarts and is race-safe: `HSETNX` is atomic, so if two
concurrent first-opens for the same brand-new identity both try to pin,
only one write wins and the other's `HSETNX` is silently a no-op -- both
callers then read back whichever one actually won, never split-brained.
"""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis

from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.proxy import ProxyEndpoint

_KEY_PREFIX = "proxy:"
_FIELD = "endpoint"
_SEP = "|"


def _serialize(proxy: ProxyEndpoint) -> str:
    return _SEP.join(
        [
            proxy.scheme,
            proxy.host,
            str(proxy.port),
            proxy.username or "",
            proxy.password or "",
            proxy.vendor or "",
        ]
    )


def _deserialize(raw: str, identity: IdentityKey) -> ProxyEndpoint:
    scheme, host, port, username, password, vendor = raw.split(_SEP)
    return ProxyEndpoint(
        scheme=scheme,
        host=host,
        port=int(port),
        username=username or None,
        password=password or None,
        vendor=vendor or None,
        sticky_key=identity,
    )


class ProxyPinner:
    def __init__(self, redis: Redis, pool: list[ProxyEndpoint]) -> None:
        if not pool:
            raise ValueError("ProxyPinner requires a non-empty pool")
        self._redis = redis
        self._pool = pool

    @property
    def pool(self) -> list[ProxyEndpoint]:
        """The configured proxy endpoints -- each is a warm-pool "tier"
        (`session.warm_pool.WarmPool`). Copy, so callers can't mutate the pin
        source."""

        return list(self._pool)

    def _pick(self, identity: IdentityKey) -> ProxyEndpoint:
        """Deterministic hash-based pick, not round-robin: needs no shared
        counter, and concurrent first-assignments for *different* identities
        never contend with each other since each only ever touches its own
        Redis key."""

        digest = hashlib.sha256(identity.slug().encode()).hexdigest()
        return self._pool[int(digest, 16) % len(self._pool)]

    async def get_or_assign(self, identity: IdentityKey) -> ProxyEndpoint:
        key = f"{_KEY_PREFIX}{identity.slug()}"
        candidate = self._pick(identity)
        await self._redis.hsetnx(key, _FIELD, _serialize(candidate))
        raw = await self._redis.hget(key, _FIELD)
        if raw is None:
            raise RuntimeError(f"proxy pin for {identity.slug()!r} vanished immediately after set")
        return _deserialize(raw.decode() if isinstance(raw, bytes) else raw, identity)

    def pick_ephemeral(self, identity: IdentityKey) -> ProxyEndpoint:
        """For a one-shot identity (`identity.is_temporary`, e.g.
        `routes/scrape.py`'s per-call minted identity) only -- `get_or_assign`
        persists a `proxy:{slug}` Redis key with no TTL, which is the right
        durability tradeoff for a warm interactive identity that will be
        reopened, but would leak one permanent, never-read-again key per call
        for an identity that by construction has a random `name` and is
        opened exactly once. `_pick()` is already fully deterministic from
        `identity.slug()` alone, so skipping the Redis round trip changes
        nothing about *which* proxy is chosen -- only whether choosing it
        needs to touch Redis at all."""

        return self._pick(identity)
