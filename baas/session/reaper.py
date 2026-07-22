"""The reaper P0 explicitly deferred (`wiring.py`'s old docstring: "P0 has no
reaper"). One background asyncio task per process, woken on an interval.
Only the reaper ever destroys a context -- `registry.release()` only ever
moves ACTIVE -> IDLE, never closes the underlying browser -- so a route
calling `release()` and the reaper later destroying the same context can
never race on *what* destroys it, only *when*.

Three independent reasons a context gets destroyed, all folded into one scan
so they share the same read of registry state:
  1. Idle-TTL: IDLE past `idle_ttl_seconds` with nothing having reclaimed it.
  2. Memory pressure: node memory over a watermark -> destroy oldest-IDLE
     (by `released_at`) first, below-TTL, until pressure clears.
  3. Per-process ceiling: a single context's Chrome process over an absolute
     RSS budget, regardless of TTL or ACTIVE/IDLE -- protects neighbors from
     one tenant's runaway tab (a 4GB SPA shouldn't be able to starve 179
     others sharing the node).
A fourth pass reclaims (not destroys) ACTIVE leases nobody renewed in time --
see `force_release`'s docstring on `Registry` for why that's IDLE, not gone.

Vault save-before-destroy (`plan.md`'s "checkpoint on release-to-IDLE, not
only at destroy") is stubbed until P2's `vault.py` exists; noted at the one
call site below rather than silently skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog

from baas.observability.metrics import (
    reaper_destroyed_total,
    reaper_lease_reclaimed_total,
)
from baas.session.lease import is_expired
from baas.session.registry import Registry
from baas.spi.driver import BrowserDriver
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, ContextState

log = structlog.get_logger(__name__)


def _read_meminfo_used_pct() -> float | None:
    """Best-effort node memory pressure from `/proc/meminfo` -- same style as
    `egress/policy.py`'s `/proc/net/route` parsing, to avoid pulling in
    `psutil` for one gauge. `MemAvailable` (not `MemFree`) already accounts
    for reclaimable cache under both cgroup v1 and v2."""

    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                digits = "".join(ch for ch in rest if ch.isdigit())
                if digits:
                    values[key] = int(digits)
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if not total or available is None:
            return None
        return (total - available) / total * 100
    except OSError:
        return None


def _read_pid_rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    digits = "".join(ch for ch in line if ch.isdigit())
                    return int(digits) / 1024 if digits else None
    except OSError:
        return None
    return None


class Reaper:
    def __init__(
        self,
        registry: Registry,
        driver: BrowserDriver,
        *,
        idle_ttl_seconds: float = 300.0,
        scan_interval_seconds: float = 15.0,
        mem_pressure_watermark_pct: float = 85.0,
        per_process_ceiling_mb: float = 4096.0,
    ) -> None:
        self._registry = registry
        self._driver = driver
        self.idle_ttl_seconds = idle_ttl_seconds
        self.scan_interval_seconds = scan_interval_seconds
        self.mem_pressure_watermark_pct = mem_pressure_watermark_pct
        self.per_process_ceiling_mb = per_process_ceiling_mb
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception:
                log.exception("reaper.scan_failed")
            await asyncio.sleep(self.scan_interval_seconds)

    async def scan_once(self) -> None:
        await self._reclaim_expired_leases()
        await self._enforce_per_process_ceiling()
        await self._reap_idle_ttl()
        await self._reap_memory_pressure()

    async def _reclaim_expired_leases(self) -> None:
        now_monotonic_entries = self._registry.snapshot()
        for identity, ctx, lease, _released_at in now_monotonic_entries:
            if lease is None or ctx.state is not ContextState.ACTIVE:
                continue
            if is_expired(lease):
                log.warning("reaper.lease_expired", identity=identity.slug())
                reaper_lease_reclaimed_total.inc()
                await self._registry.force_release(identity)

    async def _enforce_per_process_ceiling(self) -> None:
        for identity, ctx, _lease, _released_at in self._registry.snapshot():
            if ctx.pid is None:
                continue
            rss = _read_pid_rss_mb(ctx.pid)
            if rss is not None and rss > self.per_process_ceiling_mb:
                log.warning(
                    "reaper.per_process_ceiling_exceeded",
                    identity=identity.slug(),
                    rss_mb=rss,
                    ceiling_mb=self.per_process_ceiling_mb,
                )
                await self._destroy(identity, ctx, reason="per_process_ceiling")

    async def _reap_idle_ttl(self) -> None:
        now = time.monotonic()
        for identity, ctx, _lease, released_at in self._registry.snapshot():
            if ctx.state is not ContextState.IDLE or released_at is None:
                continue
            if now - released_at >= self.idle_ttl_seconds:
                await self._destroy(identity, ctx, reason="idle_ttl")

    async def _reap_memory_pressure(self) -> None:
        used_pct = _read_meminfo_used_pct()
        if used_pct is None or used_pct < self.mem_pressure_watermark_pct:
            return

        idle_oldest_first = sorted(
            (
                (identity, ctx, released_at)
                for identity, ctx, _lease, released_at in self._registry.snapshot()
                if ctx.state is ContextState.IDLE and released_at is not None
            ),
            key=lambda row: row[2],
        )
        for identity, ctx, _released_at in idle_oldest_first:
            used_pct = _read_meminfo_used_pct()
            if used_pct is None or used_pct < self.mem_pressure_watermark_pct:
                break
            log.warning(
                "reaper.memory_pressure_evict", identity=identity.slug(), used_pct=used_pct
            )
            await self._destroy(identity, ctx, reason="memory_pressure")

    async def _destroy(self, identity: IdentityKey, ctx: ContextRef, *, reason: str) -> None:
        # Vault save-on-release-to-IDLE is stubbed until P2's vault.py lands
        # (plan.md: profile dirs are a node-local cache, vault is source of
        # truth) -- P1 destroys straight from the profile-dir cache, so a
        # destroy here loses warmth but not correctness (nothing durable
        # exists yet to lose).
        evicted = await self._registry.evict(identity)
        if evicted is None:
            return
        reaper_destroyed_total.labels(reason=reason).inc()
        await self._driver.close(evicted)
