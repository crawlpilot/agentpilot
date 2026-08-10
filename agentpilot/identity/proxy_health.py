"""Per-proxy health + retirement -- a port of Pulsar's `ProxyContext` /
`ProxyEntry` retirement policy (`ProxyContext.kt:244-252`, `ProxyEntry.kt`).

Two ported reasons to retire a proxy:

- **Served too many pages.** After a cap of successful fetches a proxy is
  retired, because "the target may track the fingerprint of the crawler"
  (Pulsar's own comment). The cap carries a **±25% jitter, fixed once per
  proxy**, so retirements aren't periodic and predictable.
- **Connection losses.** Three connection failures (`ProxyEntry.isFailed =
  numConnectionLosses >= 3`) retire it as dead.

State is Redis-backed (keyed by `host:port`, so it's the *physical* proxy
across tenants), surviving restarts and shared across workers -- the same
durability rationale as `ProxyPinner`/`BurnTracker`.
"""

from __future__ import annotations

import os
import random

from redis.asyncio import Redis

from agentpilot.spi.proxy import ProxyEndpoint

_KEY_PREFIX = "proxyhealth:"
_F_SUCCESS = "successes"
_F_LOSSES = "losses"
_F_CAP = "cap"
_F_RETIRED = "retired"

_MAX_LOSSES = 3
"""`ProxyEntry.isFailed` -- three connection losses and the proxy is dead."""

_JITTER = 0.25
"""±25% jitter on the success cap (`ProxyContext.kt:244-252`)."""


def _proxy_id(proxy: ProxyEndpoint) -> str:
    return f"{proxy.host}:{proxy.port}"


class ProxyHealth:
    def __init__(self, redis: Redis, max_success: int | None = None) -> None:
        self._redis = redis
        self._max_success = max_success or int(
            os.environ.get("AGENTPILOT_PROXY_MAX_SUCCESS", "100")
        )

    def _key(self, proxy: ProxyEndpoint) -> str:
        return f"{_KEY_PREFIX}{_proxy_id(proxy)}"

    def _jittered_cap(self) -> int:
        span = int(self._max_success * _JITTER)
        return self._max_success + random.randint(-span, span)

    async def _mark_retired(self, key: str) -> None:
        await self._redis.hset(key, _F_RETIRED, "1")

    async def is_retired(self, proxy: ProxyEndpoint) -> bool:
        return (await self._redis.hget(self._key(proxy), _F_RETIRED)) in (b"1", "1")

    async def record_success(self, proxy: ProxyEndpoint) -> bool:
        """Count a served page; retire (and return True) once the jittered cap
        is reached. A no-op past retirement."""

        key = self._key(proxy)
        if await self.is_retired(proxy):
            return True
        # Fix the jittered cap once, on first success, so it's stable per proxy.
        await self._redis.hsetnx(key, _F_CAP, str(self._jittered_cap()))
        successes = int(await self._redis.hincrby(key, _F_SUCCESS, 1))
        cap_raw = await self._redis.hget(key, _F_CAP)
        cap = int(cap_raw) if cap_raw is not None else self._max_success
        if successes >= cap:
            await self._mark_retired(key)
            return True
        return False

    async def record_loss(self, proxy: ProxyEndpoint) -> bool:
        """Count a connection loss; retire (and return True) at the 3-strike
        threshold."""

        key = self._key(proxy)
        if await self.is_retired(proxy):
            return True
        losses = int(await self._redis.hincrby(key, _F_LOSSES, 1))
        if losses >= _MAX_LOSSES:
            await self._mark_retired(key)
            return True
        return False

    async def reset(self, proxy: ProxyEndpoint) -> None:
        await self._redis.delete(self._key(proxy))
