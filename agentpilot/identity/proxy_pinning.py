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

from agentpilot.identity.proxy_config import ProxyConfig
from agentpilot.identity.proxy_health import ProxyHealth
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
            proxy.tier or "",
            proxy.country or "",
        ]
    )


def _deserialize(raw: str, identity: IdentityKey) -> ProxyEndpoint:
    # Tolerate the pre-tier 6-field format alongside the current 8-field one so
    # a pin written before this change still deserializes.
    parts = raw.split(_SEP)
    parts += [""] * (8 - len(parts))
    scheme, host, port, username, password, vendor, tier, country = parts[:8]
    return ProxyEndpoint(
        scheme=scheme,
        host=host,
        port=int(port),
        username=username or None,
        password=password or None,
        vendor=vendor or None,
        tier=tier or None,
        country=country or None,
        sticky_key=identity,
    )


class ProxyPinner:
    def __init__(
        self,
        redis: Redis,
        config: ProxyConfig | list[ProxyEndpoint],
        health: ProxyHealth | None = None,
    ) -> None:
        # Accept a `ProxyConfig` (tier/tenant-aware) or a plain endpoint list
        # (back-compat -- wrapped as the `("*","*")` default pool).
        cfg = config if isinstance(config, ProxyConfig) else ProxyConfig.from_flat(config)
        if cfg.is_empty:
            raise ValueError("ProxyPinner requires a non-empty proxy config")
        self._redis = redis
        self._config = cfg
        self._health = health

    @property
    def pool(self) -> list[ProxyEndpoint]:
        """Every configured endpoint (across all tenants/tiers) -- the warm
        pool pre-launches one context per entry (`session.warm_pool.WarmPool`)."""

        return self._config.all_endpoints()

    def _pick_from(self, identity: IdentityKey, pool: list[ProxyEndpoint]) -> ProxyEndpoint:
        digest = hashlib.sha256(identity.slug().encode()).hexdigest()
        return pool[int(digest, 16) % len(pool)]

    def _pick(self, identity: IdentityKey, tier: str | None = None) -> ProxyEndpoint:
        """Deterministic hash-based pick from the pool resolved for this
        identity's tenant + requested `tier`, not round-robin: needs no shared
        counter, and concurrent first-assignments for *different* identities
        never contend since each only ever touches its own Redis key. Ignores
        retirement -- use `_healthy_pick` for the health-aware path."""

        return self._pick_from(identity, self._config.resolve(identity.tenant, tier))

    async def _healthy_pick(self, identity: IdentityKey, tier: str | None) -> ProxyEndpoint:
        """Pick, but skip retired proxies (`ProxyHealth`). Falls back to the
        full pool if every candidate is retired -- a burned exit beats no exit."""

        pool = self._config.resolve(identity.tenant, tier)
        if self._health is not None:
            healthy = [p for p in pool if not await self._health.is_retired(p)]
            if healthy:
                pool = healthy
        return self._pick_from(identity, pool)

    async def get_or_assign(
        self, identity: IdentityKey, tier: str | None = None
    ) -> ProxyEndpoint:
        key = f"{_KEY_PREFIX}{identity.slug()}"
        # If a previously pinned proxy has since retired, drop the pin so a fresh
        # (healthy) one is chosen below -- the sticky guarantee yields to the
        # retirement guarantee.
        raw = await self._redis.hget(key, _FIELD)
        if raw is not None and self._health is not None:
            pinned = _deserialize(raw.decode() if isinstance(raw, bytes) else raw, identity)
            if await self._health.is_retired(pinned):
                await self._redis.delete(key)
                raw = None
        if raw is None:
            candidate = await self._healthy_pick(identity, tier)
            await self._redis.hsetnx(key, _FIELD, _serialize(candidate))
            raw = await self._redis.hget(key, _FIELD)
        if raw is None:
            raise RuntimeError(f"proxy pin for {identity.slug()!r} vanished immediately after set")
        return _deserialize(raw.decode() if isinstance(raw, bytes) else raw, identity)

    async def pick_ephemeral(
        self, identity: IdentityKey, tier: str | None = None
    ) -> ProxyEndpoint:
        """For a one-shot identity (`identity.is_temporary`, e.g.
        `routes/scrape.py`'s per-call minted identity) only -- `get_or_assign`
        persists a `proxy:{slug}` Redis key with no TTL, which is the right
        durability tradeoff for a warm interactive identity that will be
        reopened, but would leak one permanent, never-read-again key per call
        for an identity that by construction has a random `name` and is opened
        exactly once. The pick stays deterministic from `identity.slug()`, only
        skipping retired proxies -- so it needs a Redis read for retirement but
        never a write."""

        return await self._healthy_pick(identity, tier)

    async def record_success(self, proxy: ProxyEndpoint) -> None:
        """Count a served page toward the proxy's retirement cap (no-op without
        a `ProxyHealth`)."""

        if self._health is not None:
            await self._health.record_success(proxy)

    async def record_loss(self, proxy: ProxyEndpoint) -> None:
        """Count a connection loss toward the 3-strike retirement (no-op without
        a `ProxyHealth`)."""

        if self._health is not None:
            await self._health.record_loss(proxy)
