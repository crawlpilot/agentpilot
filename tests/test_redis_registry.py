"""Unit tests for `agentpilot.session.redis_registry.RedisRegistry` -- against
`fakeredis` (with `lupa` for real Lua-script execution), not a live Redis
server, so these stay fast and Docker-free like `test_session_registry.py`.
Same behavioral contract as the in-memory `Registry` (`RegistryProtocol`) --
these tests mirror `test_session_registry.py`'s cases 1:1 to prove it."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from agentpilot.session.redis_registry import RedisRegistry
from agentpilot.spi.errors import LeaseConflict
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="a")

_next_ctx_id = iter(range(1, 10_000))


@pytest.fixture
def registry() -> RedisRegistry:
    return RedisRegistry(fakeredis.aioredis.FakeRedis())


def _make_ctx() -> ContextRef:
    n = next(_next_ctx_id)
    return ContextRef(
        context_id=f"ctx-{n}", identity=IDENTITY, state=ContextState.ACTIVE, pid=1000 + n
    )


async def _opener_counting(calls: list[int]) -> ContextRef:
    calls.append(len(calls))
    return _make_ctx()


async def test_acquire_opens_fresh_context_once(registry: RedisRegistry) -> None:
    calls: list[int] = []
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting(calls))
    assert len(calls) == 1
    assert ctx.state is ContextState.ACTIVE
    assert lease.identity == IDENTITY


async def test_second_acquire_while_active_raises_lease_conflict(registry: RedisRegistry) -> None:
    await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    with pytest.raises(LeaseConflict):
        await registry.acquire(IDENTITY, "owner2", 300.0, lambda: _opener_counting([]))


async def test_release_then_reacquire_reuses_warm_context_no_second_open(
    registry: RedisRegistry,
) -> None:
    calls: list[int] = []
    ctx1, lease1 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    await registry.release(lease1.lease_id)

    ctx2, lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert len(calls) == 1  # opener only called once across both acquires
    assert ctx2.context_id == ctx1.context_id
    assert ctx2.state is ContextState.ACTIVE
    assert lease2.lease_id != lease1.lease_id


async def test_concurrent_acquires_for_same_identity_only_one_wins(
    registry: RedisRegistry,
) -> None:
    calls: list[int] = []

    async def opener() -> ContextRef:
        await asyncio.sleep(0.01)
        return await _opener_counting(calls)

    results = await asyncio.gather(
        registry.acquire(IDENTITY, "a", 300.0, opener),
        registry.acquire(IDENTITY, "b", 300.0, opener),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, LeaseConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(calls) == 1  # only the winner actually opened a context


async def test_renew_extends_lease_and_release_clears_it(registry: RedisRegistry) -> None:
    ctx, lease = await registry.acquire(IDENTITY, "owner", 0.05, lambda: _opener_counting([]))

    renewed = await registry.renew(lease.lease_id)
    assert renewed.lease_id == lease.lease_id

    await registry.release(lease.lease_id)
    with pytest.raises(KeyError):
        await registry.renew(lease.lease_id)


async def test_evict_removes_entry_and_forgets_lease(registry: RedisRegistry) -> None:
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    await registry.release(lease.lease_id)

    evicted = await registry.evict(IDENTITY)
    assert evicted is not None
    assert evicted.context_id == ctx.context_id
    assert await registry.snapshot() == []

    calls: list[int] = []
    ctx2, _lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert len(calls) == 1
    assert ctx2.context_id != ctx.context_id


async def test_force_release_reclaims_active_lease_to_idle(registry: RedisRegistry) -> None:
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    await registry.force_release(IDENTITY)

    with pytest.raises(KeyError):
        await registry.renew(lease.lease_id)

    calls: list[int] = []
    ctx2, _lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert ctx2.context_id == ctx.context_id
    assert len(calls) == 0


async def test_snapshot_reflects_identity_fields_losslessly(registry: RedisRegistry) -> None:
    await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    snap = await registry.snapshot()
    assert len(snap) == 1
    identity, ctx, lease, released_at = snap[0]
    assert identity == IDENTITY
    assert ctx.state is ContextState.ACTIVE
    assert lease is not None
    assert released_at is None


async def test_opener_failure_releases_the_reservation(registry: RedisRegistry) -> None:
    async def failing_opener() -> ContextRef:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await registry.acquire(IDENTITY, "owner", 300.0, failing_opener)

    # The reservation must not be left stuck ACTIVE -- a fresh acquire should
    # succeed immediately rather than raising LeaseConflict forever.
    ctx, _lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    assert ctx.state is ContextState.ACTIVE
