"""`InMemoryStateStore` and `RedisStateStore` must be behaviourally identical.

Phase 3 moved burn accounting, proxy health and proxy pinning off a direct
`redis.asyncio.Redis` and onto the `StateStore` seam, so the browser layer no
longer depends on Redis. That is only safe if the shipped in-memory default
means exactly what the Redis-backed one means -- otherwise a single-process
crawler and a multi-worker deployment would silently disagree about when an
identity is burned or a proxy is retired.

Every test here runs against both implementations. A divergence is a real bug:
the in-memory store is what an embedding caller gets by default.
"""

from __future__ import annotations

from collections.abc import Callable

import fakeredis
import pytest

from agentpilot.control.identity import identity_for
from agentpilot.control.redis_store import RedisStateStore
from agentpilot.identity.burn_tracker import MAX_WARNINGS, BurnTracker
from agentpilot.identity.proxy_health import ProxyHealth
from agentpilot.policy import InMemoryStateStore, StateStore
from agentpilot.spi.proxy import ProxyEndpoint

IDENTITY = identity_for("t", "d", "n")
PROXY = ProxyEndpoint(scheme="http", host="p", port=1)

STORES: list[tuple[str, Callable[[], StateStore]]] = [
    ("memory", InMemoryStateStore),
    ("redis", lambda: RedisStateStore(fakeredis.aioredis.FakeRedis())),
]
IDS = [name for name, _ in STORES]


@pytest.fixture(params=[factory for _, factory in STORES], ids=IDS)
def store(request: pytest.FixtureRequest) -> StateStore:
    return request.param()  # type: ignore[no-any-return]


# --------------------------------------------------------------- primitives


async def test_counters_round_trip(store: StateStore) -> None:
    assert await store.get_int("k") is None
    assert await store.incr_by("k", 5) == 5
    assert await store.incr_by("k", -2) == 3
    assert await store.get_int("k") == 3
    await store.set_int("k", 0)
    assert await store.get_int("k") == 0
    await store.delete("k")
    assert await store.get_int("k") is None


async def test_hsetnx_only_writes_once(store: StateStore) -> None:
    """The jittered retirement cap depends on this: `ProxyHealth` fixes the cap
    on first success and must never overwrite it afterwards."""

    assert await store.hsetnx("h", "cap", "10") is True
    assert await store.hsetnx("h", "cap", "99") is False
    assert await store.hget("h", "cap") == "10"


async def test_hget_returns_str_not_bytes(store: StateStore) -> None:
    """Redis returns bytes; the seam normalises to `str` so callers do not
    carry a `.decode()` branch (they used to, and it was easy to forget)."""

    await store.hset("h", "f", "v")
    got = await store.hget("h", "f")
    assert got == "v"
    assert isinstance(got, str)


async def test_hincr_by_starts_from_zero(store: StateStore) -> None:
    assert await store.hincr_by("h", "n", 2) == 2
    assert await store.hincr_by("h", "n", 3) == 5


async def test_missing_field_and_key_are_none(store: StateStore) -> None:
    assert await store.hget("nope", "f") is None
    await store.hset("h", "f", "v")
    assert await store.hget("h", "other") is None


# ------------------------------------------------------- policy on top of it


async def test_burn_accounting_agrees(store: StateStore) -> None:
    tracker = BurnTracker(store)
    assert await tracker.warnings(IDENTITY) == 0
    await tracker.record_block(IDENTITY, MAX_WARNINGS)
    assert await tracker.is_burned(IDENTITY) is True
    await tracker.reset(IDENTITY)
    assert await tracker.warnings(IDENTITY) == 0


async def test_success_decrement_floors_at_zero(store: StateStore) -> None:
    """The self-heal must never go negative -- a negative total would take
    several blocks just to climb back to zero before it could burn again."""

    tracker = BurnTracker(store)
    assert await tracker.record_success(IDENTITY) == 0
    assert await tracker.warnings(IDENTITY) == 0


async def test_proxy_retirement_agrees(store: StateStore) -> None:
    health = ProxyHealth(store, max_success=1)
    assert await health.is_retired(PROXY) is False
    assert await health.record_success(PROXY) is True
    assert await health.is_retired(PROXY) is True
    await health.reset(PROXY)
    assert await health.is_retired(PROXY) is False
