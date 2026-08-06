"""Unit tests for the warm-session pool + keepalive loop
(`agentpilot.session.warm_pool`). A fake driver stands in for Chromium; no
browser. Deterministic: tests drive `tick()`/`take()` directly rather than the
background loop."""

from __future__ import annotations

import uuid
from pathlib import Path

from agentpilot.session.registry import Registry
from agentpilot.session.warm_pool import KeepaliveLoop, WarmPool, tier_key
from agentpilot.spi.lease import ContextRef, ContextState
from agentpilot.spi.proxy import ProxyEndpoint

_PROXY_A = ProxyEndpoint(scheme="http", host="a.example", port=1)
_PROXY_B = ProxyEndpoint(scheme="http", host="b.example", port=2)


class FakeDriver:
    """Implements just the `BrowserDriver` methods the warm pool / keepalive /
    acquire paths call. Tracks opens, closes, liveness, and the proxy each
    context was launched with."""

    def __init__(self, *, open_alive: bool = True) -> None:
        self.open_calls = 0
        self.closed: list[str] = []
        self.alive: dict[str, bool] = {}
        self.proxy_of: dict[str, ProxyEndpoint | None] = {}
        self._open_alive = open_alive

    async def open(self, identity, profile_dir, proxy, headful, egress, **kw) -> ContextRef:  # noqa: ANN001
        self.open_calls += 1
        cid = uuid.uuid4().hex
        ctx = ContextRef(context_id=cid, identity=identity, state=ContextState.ACTIVE, pid=None)
        self.alive[cid] = self._open_alive
        self.proxy_of[cid] = proxy
        return ctx

    async def close(self, ctx: ContextRef) -> None:
        self.closed.append(ctx.context_id)
        self.alive[ctx.context_id] = False

    async def is_alive(self, ctx: ContextRef) -> bool:
        return self.alive.get(ctx.context_id, False)

    async def keepalive(self, ctx: ContextRef) -> bool:
        return self.alive.get(ctx.context_id, False)


def _pool(driver: FakeDriver, tmp: Path, *, target: int = 2) -> WarmPool:
    return WarmPool(driver, [_PROXY_A, _PROXY_B], profiles_root=tmp, target_per_tier=target)


async def test_refill_maintains_target_per_tier(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=2)
    await pool.tick()
    assert pool.ready_count(_PROXY_A) == 2
    assert pool.ready_count(_PROXY_B) == 2
    assert driver.open_calls == 4
    # Idempotent once full.
    await pool.tick()
    assert driver.open_calls == 4


async def test_take_returns_tier_matched_context_then_none(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=2)
    await pool.tick()

    a1 = await pool.take(_PROXY_A)
    assert a1 is not None and driver.proxy_of[a1.context_id] == _PROXY_A
    assert pool.ready_count(_PROXY_A) == 1
    a2 = await pool.take(_PROXY_A)
    assert a2 is not None
    assert await pool.take(_PROXY_A) is None  # drained
    # Other tier untouched.
    assert pool.ready_count(_PROXY_B) == 2


async def test_refill_after_take(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=2)
    await pool.tick()
    await pool.take(_PROXY_A)
    assert pool.ready_count(_PROXY_A) == 1
    await pool.tick()
    assert pool.ready_count(_PROXY_A) == 2


async def test_target_zero_is_inert(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = WarmPool(driver, [_PROXY_A], profiles_root=tmp_path, target_per_tier=0)
    await pool.tick()
    assert driver.open_calls == 0
    assert await pool.take(_PROXY_A) is None


async def test_skips_warming_under_memory_pressure(tmp_path: Path, monkeypatch) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=2)
    monkeypatch.setattr("agentpilot.session.warm_pool.read_meminfo_used_pct", lambda: 99.0)
    await pool.tick()
    assert driver.open_calls == 0  # pressure -> no speculative warming


async def test_take_skips_and_destroys_dead_pooled_context(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=1)
    await pool.tick()
    # Kill the one ready context; take must not hand back a zombie.
    (ready_ctx,) = pool._ready[tier_key(_PROXY_A)]
    driver.alive[ready_ctx.ctx.context_id] = False
    assert await pool.take(_PROXY_A) is None
    assert ready_ctx.ctx.context_id in driver.closed


async def test_prune_dead_ready_on_tick(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=2)
    await pool.tick()
    victim = pool._ready[tier_key(_PROXY_A)][0]
    driver.alive[victim.ctx.context_id] = False
    await pool.tick()  # prunes the dead one, refills back to target
    assert pool.ready_count(_PROXY_A) == 2
    assert victim.ctx.context_id in driver.closed


async def test_sweep_taken_cleans_after_close(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=1)
    await pool.tick()
    taken = await pool.take(_PROXY_A)
    assert taken is not None
    assert taken.context_id in pool._taken
    await driver.close(taken)  # caller closed the adopted context
    await pool.tick()  # sweep notices it's dead and drops its bookkeeping
    assert taken.context_id not in pool._taken


async def test_drain_destroys_ready(tmp_path: Path) -> None:
    driver = FakeDriver()
    pool = _pool(driver, tmp_path, target=2)
    await pool.tick()
    await pool.drain()
    assert pool.ready_count(_PROXY_A) == 0 and pool.ready_count(_PROXY_B) == 0
    assert len(driver.closed) == 4


# --------------------------------------------------------------- keepalive loop


async def test_keepalive_evicts_dead_idle_registry_context(tmp_path: Path) -> None:
    from agentpilot.spi.identity import IdentityKey

    driver = FakeDriver()
    registry = Registry()
    identity = IdentityKey(tenant="t", domain="d", name="n")

    async def opener() -> ContextRef:
        return await driver.open(identity, tmp_path, None, False, None)

    ctx, lease = await registry.acquire(identity, "owner", 300.0, opener)
    await registry.release(lease.lease_id)  # ACTIVE -> IDLE
    driver.alive[ctx.context_id] = False  # died while idle

    loop = KeepaliveLoop(registry, driver)
    await loop.tick()

    assert await registry.snapshot() == []  # evicted
    assert ctx.context_id in driver.closed


async def test_keepalive_keeps_live_idle_context(tmp_path: Path) -> None:
    from agentpilot.spi.identity import IdentityKey

    driver = FakeDriver()
    registry = Registry()
    identity = IdentityKey(tenant="t", domain="d", name="n")

    async def opener() -> ContextRef:
        return await driver.open(identity, tmp_path, None, False, None)

    ctx, lease = await registry.acquire(identity, "owner", 300.0, opener)
    await registry.release(lease.lease_id)

    loop = KeepaliveLoop(registry, driver)
    await loop.tick()

    snap = await registry.snapshot()
    assert len(snap) == 1 and snap[0][1].context_id == ctx.context_id
    assert ctx.context_id not in driver.closed
