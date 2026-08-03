"""`agentpilot.identity.proxy_pinning` -- assign-once-keep-for-life, race-safe via
Redis `HSETNX`. Against `fakeredis`, same pattern as `test_redis_registry.py`."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.proxy import ProxyEndpoint

POOL = [
    ProxyEndpoint(scheme="http", host="proxy1.example.com", port=8080, vendor="acme"),
    ProxyEndpoint(scheme="http", host="proxy2.example.com", port=8080, vendor="acme"),
    ProxyEndpoint(scheme="http", host="proxy3.example.com", port=8080, vendor="acme"),
]

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="alice")


@pytest.fixture
def pinner() -> ProxyPinner:
    return ProxyPinner(fakeredis.aioredis.FakeRedis(), POOL)


async def test_get_or_assign_is_stable_across_repeated_calls(pinner: ProxyPinner) -> None:
    first = await pinner.get_or_assign(IDENTITY)
    second = await pinner.get_or_assign(IDENTITY)
    assert first == second


async def test_get_or_assign_survives_a_fresh_pinner_instance_same_redis() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    first = await ProxyPinner(redis, POOL).get_or_assign(IDENTITY)
    # A brand-new ProxyPinner (simulating a process restart) backed by the
    # *same* Redis must still return the pinned proxy, not re-roll one.
    second = await ProxyPinner(redis, POOL).get_or_assign(IDENTITY)
    assert first == second


async def test_different_identities_can_get_different_proxies() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    pinner = ProxyPinner(redis, POOL)
    assignments = {
        await pinner.get_or_assign(IdentityKey(tenant="t", domain="example.com", name=f"user{i}"))
        for i in range(20)
    }
    # Not asserting *all* pool entries get used (hash collisions are legal)
    # -- just that pinning isn't collapsing everyone onto a single proxy.
    assert len({p.host for p in assignments}) > 1


async def test_concurrent_first_assignment_is_race_safe(pinner: ProxyPinner) -> None:
    results = await asyncio.gather(*(pinner.get_or_assign(IDENTITY) for _ in range(10)))
    assert len(set(results)) == 1  # every concurrent caller agrees on the same pin


def test_empty_pool_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        ProxyPinner(fakeredis.aioredis.FakeRedis(), [])


def test_pick_ephemeral_matches_the_deterministic_pick_get_or_assign_would_persist(
    pinner: ProxyPinner,
) -> None:
    # Same proxy either way -- the only difference is whether choosing it
    # touches Redis, not which one gets chosen.
    assert pinner.pick_ephemeral(IDENTITY) == pinner._pick(IDENTITY)


def test_pick_ephemeral_never_writes_to_redis() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    pinner = ProxyPinner(redis, POOL)
    pinner.pick_ephemeral(IDENTITY)
    assert asyncio.run(redis.exists(f"proxy:{IDENTITY.slug()}")) == 0
