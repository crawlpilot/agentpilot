"""Unit tests for `baas.session.reaper` -- a fake driver (records `close()`
calls) and monkeypatched `/proc` readers stand in for a real container, so
these run without Docker/Patchright."""

from __future__ import annotations

import asyncio

import pytest

import baas.session.reaper as reaper_module
from baas.session.reaper import Reaper
from baas.session.registry import Registry
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, ContextState


class FakeDriver:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def close(self, ctx: ContextRef) -> None:
        self.closed.append(ctx.context_id)


def _identity(name: str) -> IdentityKey:
    return IdentityKey(tenant="t", domain="example.com", name=name)


async def _make_idle_entry(
    registry: Registry, identity: IdentityKey, pid: int | None = None
) -> ContextRef:
    async def opener() -> ContextRef:
        return ContextRef(
            context_id=f"ctx-{identity.name}", identity=identity, state=ContextState.ACTIVE, pid=pid
        )

    ctx, lease = await registry.acquire(identity, "owner", 300.0, opener)
    await registry.release(lease.lease_id)
    return ctx


async def test_idle_ttl_destroys_context_past_ttl() -> None:
    registry = Registry()
    driver = FakeDriver()
    ctx = await _make_idle_entry(registry, _identity("a"))
    reaper = Reaper(registry, driver, idle_ttl_seconds=0.01)

    await asyncio.sleep(0.02)
    await reaper.scan_once()

    assert driver.closed == [ctx.context_id]
    assert await registry.snapshot() == []


async def test_idle_ttl_leaves_fresh_idle_context_alone() -> None:
    registry = Registry()
    driver = FakeDriver()
    await _make_idle_entry(registry, _identity("a"))
    reaper = Reaper(registry, driver, idle_ttl_seconds=300.0)

    await reaper.scan_once()

    assert driver.closed == []
    assert len(await registry.snapshot()) == 1


async def test_lease_expiry_force_releases_without_destroying() -> None:
    registry = Registry()
    driver = FakeDriver()

    async def opener() -> ContextRef:
        return ContextRef(
            context_id="ctx-active", identity=_identity("a"), state=ContextState.ACTIVE, pid=None
        )

    identity = _identity("a")
    ctx, _lease = await registry.acquire(identity, "owner", 0.01, opener)
    reaper = Reaper(registry, driver, idle_ttl_seconds=300.0)

    await asyncio.sleep(0.02)
    await reaper.scan_once()

    assert driver.closed == []  # reclaimed to IDLE, not destroyed
    assert ctx.state is ContextState.IDLE


async def test_per_process_ceiling_destroys_regardless_of_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry()
    driver = FakeDriver()

    async def opener() -> ContextRef:
        return ContextRef(
            context_id="ctx-fat", identity=_identity("a"), state=ContextState.ACTIVE, pid=4242
        )

    ctx, _lease = await registry.acquire(_identity("a"), "owner", 300.0, opener)
    monkeypatch.setattr(reaper_module, "_read_pid_rss_mb", lambda pid: 8192.0)
    reaper = Reaper(registry, driver, per_process_ceiling_mb=4096.0, idle_ttl_seconds=300.0)

    await reaper.scan_once()

    assert driver.closed == [ctx.context_id]


async def test_memory_pressure_evicts_oldest_idle_first(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry()
    driver = FakeDriver()

    older = await _make_idle_entry(registry, _identity("older"))
    await asyncio.sleep(0.01)
    newer = await _make_idle_entry(registry, _identity("newer"))

    # Simulate memory pressure that clears after exactly one eviction.
    pct_sequence = iter([90.0, 90.0, 40.0])
    monkeypatch.setattr(reaper_module, "_read_meminfo_used_pct", lambda: next(pct_sequence))
    reaper = Reaper(registry, driver, idle_ttl_seconds=300.0, mem_pressure_watermark_pct=85.0)

    await reaper.scan_once()

    assert driver.closed == [older.context_id]
    remaining_ids = [
        ctx.context_id for _identity, ctx, _lease, _released_at in await registry.snapshot()
    ]
    assert remaining_ids == [newer.context_id]
