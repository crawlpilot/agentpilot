"""Unit tests for `agentpilot.session.acquire.acquire_validated` -- validate-on-
acquire + auto-restart, over the real in-memory `Registry` with a fake driver."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from agentpilot.session.acquire import acquire_validated
from agentpilot.session.registry import Registry
from agentpilot.spi.errors import ContextCrashed, LeaseConflict
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState

_IDENTITY = IdentityKey(tenant="t", domain="d", name="n")


class FakeDriver:
    def __init__(self, *, open_alive: bool = True) -> None:
        self.open_calls = 0
        self.closed: list[str] = []
        self.alive: dict[str, bool] = {}
        self._open_alive = open_alive

    async def open(self, identity, profile_dir, proxy, headful, egress, **kw) -> ContextRef:  # noqa: ANN001
        self.open_calls += 1
        cid = uuid.uuid4().hex
        ctx = ContextRef(context_id=cid, identity=identity, state=ContextState.ACTIVE, pid=None)
        self.alive[cid] = self._open_alive
        return ctx

    async def close(self, ctx: ContextRef) -> None:
        self.closed.append(ctx.context_id)
        self.alive[ctx.context_id] = False

    async def is_alive(self, ctx: ContextRef) -> bool:
        return self.alive.get(ctx.context_id, False)


def _opener(driver: FakeDriver, tmp: Path):
    async def opener() -> ContextRef:
        return await driver.open(_IDENTITY, tmp, None, False, None)

    return opener


async def test_fresh_open_returned_without_reopen(tmp_path: Path) -> None:
    driver = FakeDriver()
    registry = Registry()
    ctx, _lease = await acquire_validated(
        registry=registry,
        driver=driver,
        identity=_IDENTITY,
        owner="o",
        ttl_seconds=300.0,
        opener=_opener(driver, tmp_path),
    )
    assert driver.open_calls == 1
    assert await driver.is_alive(ctx)


async def test_dead_reused_context_is_reopened(tmp_path: Path) -> None:
    driver = FakeDriver()
    registry = Registry()
    opener = _opener(driver, tmp_path)

    # First acquire + release parks a warm IDLE context.
    ctx1, lease1 = await registry.acquire(_IDENTITY, "o", 300.0, opener)
    await registry.release(lease1.lease_id)
    driver.alive[ctx1.context_id] = False  # it died while idle

    ctx2, _lease2 = await acquire_validated(
        registry=registry,
        driver=driver,
        identity=_IDENTITY,
        owner="o",
        ttl_seconds=300.0,
        opener=opener,
    )
    # The dead one was evicted + closed, and a fresh live context opened.
    assert ctx1.context_id in driver.closed
    assert ctx2.context_id != ctx1.context_id
    assert await driver.is_alive(ctx2)
    assert driver.open_calls == 2  # original + reopen


async def test_raises_when_reopen_keeps_failing(tmp_path: Path) -> None:
    driver = FakeDriver(open_alive=False)  # every context is born dead
    registry = Registry()
    with pytest.raises(ContextCrashed):
        await acquire_validated(
            registry=registry,
            driver=driver,
            identity=_IDENTITY,
            owner="o",
            ttl_seconds=300.0,
            opener=_opener(driver, tmp_path),
            max_attempts=2,
        )
    assert driver.open_calls == 2  # bounded retries


async def test_lease_conflict_propagates(tmp_path: Path) -> None:
    driver = FakeDriver()
    registry = Registry()
    opener = _opener(driver, tmp_path)
    # Hold an ACTIVE lease, then a second acquire for the same identity conflicts.
    await registry.acquire(_IDENTITY, "o", 300.0, opener)
    with pytest.raises(LeaseConflict):
        await acquire_validated(
            registry=registry,
            driver=driver,
            identity=_IDENTITY,
            owner="o2",
            ttl_seconds=300.0,
            opener=opener,
        )
