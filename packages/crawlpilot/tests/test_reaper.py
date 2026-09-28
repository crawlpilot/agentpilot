"""Unit tests for `crawlpilot.session.reaper` -- a fake driver (records `close()`
calls) and monkeypatched `/proc` readers stand in for a real container, so
these run without Docker/Patchright."""

from __future__ import annotations

import asyncio

import pytest

import crawlpilot.session.reaper as reaper_module
from crawlpilot.session.reaper import Reaper
from crawlpilot.session.registry import Registry
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState


class FakeDriver:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def close(self, ctx: ContextRef) -> None:
        self.closed.append(ctx.context_id)


def _identity(name: str) -> IdentityRef:
    return IdentityRef(key=f"t/example.com/{name}")


async def _make_idle_entry(
    registry: Registry, identity: IdentityRef, pid: int | None = None
) -> ContextRef:
    async def opener() -> ContextRef:
        return ContextRef(
            context_id=f"ctx-{identity.key.split('/')[-1]}",
            identity=identity,
            state=ContextState.ACTIVE,
            pid=pid,
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
    monkeypatch.setattr(reaper_module, "read_pid_rss_mb", lambda pid: 8192.0)
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
    monkeypatch.setattr(reaper_module, "read_meminfo_used_pct", lambda: next(pct_sequence))
    reaper = Reaper(registry, driver, idle_ttl_seconds=300.0, mem_pressure_watermark_pct=85.0)

    await reaper.scan_once()

    assert driver.closed == [older.context_id]
    remaining_ids = [
        ctx.context_id for _identity, ctx, _lease, _released_at in await registry.snapshot()
    ]
    assert remaining_ids == [newer.context_id]


# --- a browser is a process TREE, not a process -----------------------------


def test_tree_rss_sums_a_process_and_its_children(tmp_path, monkeypatch) -> None:
    """MEASURED, inside a worker container: Chrome's root process held 292 MB
    while each of its children held a few MB. A 4096 MB ceiling read against the
    root alone was therefore unreachable by any real page -- the node filled to
    6.7 of 7.65 GB, the reaper had nothing to say, and a tab died with
    `page_crash`. The tree those children belong to was over a gigabyte.
    """

    from crawlpilot.session import reaper as reaper_mod

    rss = {1: 290.0, 2: 400.0, 3: 350.0, 4: 60.0}
    children = {1: [2, 3], 2: [4], 3: [], 4: []}

    monkeypatch.setattr(reaper_mod, "read_pid_rss_mb", lambda pid: rss.get(pid))
    monkeypatch.setattr(reaper_mod, "_child_pids", lambda pid: children.get(pid, []))

    assert reaper_mod.read_tree_rss_mb(1) == 1100.0
    # The root alone is what the ceiling used to see, and it is a quarter of the
    # truth.
    assert reaper_mod.read_pid_rss_mb(1) == 290.0


def test_tree_rss_survives_a_process_exiting_mid_walk(monkeypatch) -> None:
    """A browser being torn down while this reads it is the ordinary case, not
    an error: the child is gone, its RSS is unknowable, and the sum is still
    the best answer available."""

    from crawlpilot.session import reaper as reaper_mod

    monkeypatch.setattr(
        reaper_mod, "read_pid_rss_mb", lambda pid: 100.0 if pid == 1 else None
    )
    monkeypatch.setattr(reaper_mod, "_child_pids", lambda pid: [2, 3] if pid == 1 else [])

    assert reaper_mod.read_tree_rss_mb(1) == 100.0


def test_tree_rss_is_none_when_nothing_is_readable(monkeypatch) -> None:
    """Off Linux, or for a pid that is already gone. `None` means "no answer",
    which the ceiling check skips -- distinct from 0.0, which would mean "this
    browser is using nothing" and is never true."""

    from crawlpilot.session import reaper as reaper_mod

    monkeypatch.setattr(reaper_mod, "read_pid_rss_mb", lambda pid: None)
    monkeypatch.setattr(reaper_mod, "_child_pids", lambda pid: [])

    assert reaper_mod.read_tree_rss_mb(999) is None


def test_tree_rss_does_not_loop_on_a_cycle(monkeypatch) -> None:
    """`/proc` should never report one, but a pid counted twice would inflate
    the ceiling and evict a healthy browser."""

    from crawlpilot.session import reaper as reaper_mod

    monkeypatch.setattr(reaper_mod, "read_pid_rss_mb", lambda pid: 50.0)
    monkeypatch.setattr(reaper_mod, "_child_pids", lambda pid: {1: [2], 2: [1]}.get(pid, []))

    assert reaper_mod.read_tree_rss_mb(1) == 100.0
