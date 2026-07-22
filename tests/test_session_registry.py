"""Unit tests for `baas.session.registry` -- no browser, no driver: `open()`
is a fake `opener` callback returning a bare `ContextRef`, matching how
`routes/sessions.py` only ever calls `driver.open()` from inside one."""

from __future__ import annotations

import asyncio

import pytest

from baas.session.lease import is_expired
from baas.session.registry import Registry
from baas.spi.errors import LeaseConflict
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, ContextState

IDENTITY = IdentityKey(tenant="t", domain="example.com", name="a")


def _make_ctx(n: int = 0) -> ContextRef:
    return ContextRef(context_id=f"ctx-{n}", identity=IDENTITY, state=ContextState.ACTIVE, pid=None)


async def _opener_counting(calls: list[int]) -> ContextRef:
    calls.append(len(calls))
    return _make_ctx(len(calls))


async def test_acquire_opens_fresh_context_once() -> None:
    registry = Registry()
    calls: list[int] = []
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting(calls))
    assert len(calls) == 1
    assert ctx.state is ContextState.ACTIVE
    assert lease.identity == IDENTITY


async def test_second_acquire_while_active_raises_lease_conflict() -> None:
    registry = Registry()
    await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    with pytest.raises(LeaseConflict):
        await registry.acquire(IDENTITY, "owner2", 300.0, lambda: _opener_counting([]))


async def test_release_then_reacquire_reuses_warm_context_no_second_open() -> None:
    """The exact bug found via real UI use in P0: reopening the same identity
    after a release must not call `opener()` (i.e. must not launch a second
    Chrome onto the same profile dir)."""

    registry = Registry()
    calls: list[int] = []
    ctx1, lease1 = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting(calls))
    await registry.release(lease1.lease_id)

    ctx2, lease2 = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting(calls))
    assert len(calls) == 1  # opener only called once across both acquires
    assert ctx2 is ctx1
    assert ctx2.state is ContextState.ACTIVE
    assert lease2.lease_id != lease1.lease_id


async def test_concurrent_acquires_for_same_identity_only_one_wins() -> None:
    registry = Registry()
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


async def test_renew_extends_lease_and_release_clears_it() -> None:
    registry = Registry()
    ctx, lease = await registry.acquire(IDENTITY, "owner", 0.05, lambda: _opener_counting([]))
    assert not is_expired(lease)

    await asyncio.sleep(0.03)
    renewed = await registry.renew(lease.lease_id)
    assert renewed.lease_id == lease.lease_id
    assert not is_expired(renewed)

    await registry.release(lease.lease_id)
    with pytest.raises(KeyError):
        await registry.renew(lease.lease_id)


async def test_evict_removes_entry_and_forgets_lease() -> None:
    registry = Registry()
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    await registry.release(lease.lease_id)

    evicted = await registry.evict(IDENTITY)
    assert evicted is ctx
    assert await registry.snapshot() == []

    # A fresh acquire after eviction must open a *new* context, not reuse.
    calls: list[int] = []
    ctx2, _lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert len(calls) == 1
    assert ctx2 is not ctx


async def test_force_release_reclaims_active_lease_to_idle() -> None:
    registry = Registry()
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    await registry.force_release(IDENTITY)

    assert ctx.state is ContextState.IDLE
    with pytest.raises(KeyError):
        await registry.renew(lease.lease_id)

    # Warm context is still reusable afterward.
    calls: list[int] = []
    ctx2, _lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert ctx2 is ctx
    assert len(calls) == 0
