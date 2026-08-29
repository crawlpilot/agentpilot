"""The shared-state driver seam.

One `StateStore` Protocol rather than a `BurnStore` / `ProxyHealthStore` /
`ProxyPinStore` trio: all three callers want the same thing -- keyed counters
and hash fields with a TTL -- so three role-named Protocols would have been the
same ten methods written three times, and every backend would implement them
three times too. The *roles* stay distinct where they belong, in the classes
that hold the policy.

`InMemoryStateStore` is the default and is correct for a single process only.
A multi-worker deployment MUST inject a shared implementation
(`agentpilot.control.redis_store.RedisStateStore`); two workers each keeping
private burn counters would silently stop retiring burned identities. The
composition root asserts this -- see `gateway.wiring`.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class StateStore(Protocol):
    """Keyed counters and hash fields with expiry. Deliberately shaped like the
    Redis subset the previous implementations used, so the Redis-backed version
    is a thin pass-through and the semantics cannot drift."""

    async def incr_by(self, key: str, delta: int) -> int: ...
    async def get_int(self, key: str) -> int | None: ...
    async def set_int(self, key: str, value: int) -> None: ...
    async def expire(self, key: str, ttl_seconds: int) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def hget(self, key: str, field: str) -> str | None: ...
    async def hset(self, key: str, field: str, value: str) -> None: ...
    async def hsetnx(self, key: str, field: str, value: str) -> bool: ...
    async def hincr_by(self, key: str, field: str, delta: int) -> int: ...


class InMemoryStateStore:
    """Process-local `StateStore`. Single-process use only -- see module docstring.

    TTLs are honoured lazily on read rather than by a sweeper: the counters here
    are small and short-lived, and a background task would be a surprising thing
    for a library to start on a caller's event loop.
    """

    def __init__(self) -> None:
        self._values: dict[str, int] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._expiry: dict[str, float] = {}

    # ------------------------------------------------------------ expiry

    def _expired(self, key: str) -> bool:
        at = self._expiry.get(key)
        if at is None or at > time.monotonic():
            return False
        self._values.pop(key, None)
        self._hashes.pop(key, None)
        self._expiry.pop(key, None)
        return True

    # ----------------------------------------------------------- counters

    async def incr_by(self, key: str, delta: int) -> int:
        self._expired(key)
        total = self._values.get(key, 0) + delta
        self._values[key] = total
        return total

    async def get_int(self, key: str) -> int | None:
        self._expired(key)
        return self._values.get(key)

    async def set_int(self, key: str, value: int) -> None:
        self._expired(key)
        self._values[key] = value

    async def expire(self, key: str, ttl_seconds: int) -> None:
        self._expiry[key] = time.monotonic() + ttl_seconds

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._values.pop(key, None)
            self._hashes.pop(key, None)
            self._expiry.pop(key, None)

    # -------------------------------------------------------------- hashes

    async def hget(self, key: str, field: str) -> str | None:
        self._expired(key)
        return self._hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str) -> None:
        self._expired(key)
        self._hashes.setdefault(key, {})[field] = value

    async def hsetnx(self, key: str, field: str, value: str) -> bool:
        self._expired(key)
        fields = self._hashes.setdefault(key, {})
        if field in fields:
            return False
        fields[field] = value
        return True

    async def hincr_by(self, key: str, field: str, delta: int) -> int:
        self._expired(key)
        fields = self._hashes.setdefault(key, {})
        total = int(fields.get(field, "0")) + delta
        fields[field] = str(total)
        return total
