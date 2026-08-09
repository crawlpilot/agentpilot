"""Per-identity burn accounting -- a port of Pulsar's privacy-context leak
accounting (`AbstractPrivacyContext.kt:50-76,330-347`).

A warm identity that keeps getting walled is "burned": its cookies/profile are
a known-bad signal, so it should be retired and started fresh rather than
reused. This tracks a weighted warning counter per identity in Redis
(surviving restarts, shared across worker processes):

- a block adds its weight (robot-check `+2`, a hard FORBIDDEN `+8` = instant
  retire -- the driver computes the weight, see `block_detect.warning_weight`),
- a success decrements by one (the self-healing behaviour from the source), and
- once warnings reach `MAX_WARNINGS` the identity is burned.

Only *warm* identities (a stable `session_name`) are tracked -- a throwaway
scrape identity is one-shot and deleted on teardown anyway, so there is nothing
to burn. A burned warm identity is retired by deleting its profile dir (cookies)
and clearing this counter, so its next open is a clean first-visit browser.
"""

from __future__ import annotations

from redis.asyncio import Redis

from agentpilot.spi.identity import IdentityKey

MAX_WARNINGS = 8
"""Retire threshold -- Pulsar's `PRIVACY_MAX_WARNINGS`. Must stay in step with
`extraction.block_detect.MAX_WARNINGS`, which sizes the instant-retire FORBIDDEN
weight to exactly this."""

_KEY_PREFIX = "burn:"
_TTL_SECONDS = 1800
"""Warnings decay after 30 min of inactivity (Pulsar's context idle timeout) --
an identity that hasn't been walled in a while gets a clean slate on its own."""

MINOR_WARNING_FACTOR = 5
"""Pulsar's `PRIVACY_MINOR_WARNING_FACTOR` (`AbstractPrivacyContext.markMinorWarning`,
:211-218): a soft CRAWL-scope failure is a *minor* warning; every this-many
minor warnings convert into one real warning. Keeps a run of thin/rate-limited
pages from burning a warm identity as fast as a genuine block would."""


class BurnTracker:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, identity: IdentityKey) -> str:
        return f"{_KEY_PREFIX}{identity.slug()}"

    def _minor_key(self, identity: IdentityKey) -> str:
        return f"{_KEY_PREFIX}{identity.slug()}:minor"

    async def record_block(self, identity: IdentityKey, weight: int) -> int:
        """Add a block's weight; returns the new warning total."""
        if weight <= 0:
            return await self.warnings(identity)
        key = self._key(identity)
        total = await self._redis.incrby(key, weight)
        await self._redis.expire(key, _TTL_SECONDS)
        return int(total)

    async def record_minor_block(self, identity: IdentityKey) -> int:
        """Record a soft (CRAWL-scope) failure as a minor warning. Every
        `MINOR_WARNING_FACTOR` minor warnings convert to one real warning
        (resetting the minor counter). Returns the current real-warning total."""
        mkey = self._minor_key(identity)
        minor = await self._redis.incr(mkey)
        await self._redis.expire(mkey, _TTL_SECONDS)
        if int(minor) >= MINOR_WARNING_FACTOR:
            await self._redis.delete(mkey)
            return await self.record_block(identity, 1)
        return await self.warnings(identity)

    async def record_success(self, identity: IdentityKey) -> int:
        """Self-heal: decrement one warning, floored at zero."""
        key = self._key(identity)
        total = await self._redis.decr(key)
        if total < 0:
            await self._redis.set(key, 0)
            total = 0
        else:
            await self._redis.expire(key, _TTL_SECONDS)
        return int(total)

    async def warnings(self, identity: IdentityKey) -> int:
        raw = await self._redis.get(self._key(identity))
        return int(raw) if raw is not None else 0

    async def is_burned(self, identity: IdentityKey) -> bool:
        return await self.warnings(identity) >= MAX_WARNINGS

    async def reset(self, identity: IdentityKey) -> None:
        await self._redis.delete(self._key(identity), self._minor_key(identity))
