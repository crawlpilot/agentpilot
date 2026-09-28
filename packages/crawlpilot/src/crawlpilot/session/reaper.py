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

Vault checkpointing (`plan.md`'s "checkpoint on release-to-IDLE, not only
at destroy") happens at *release* time (the gateway/worker's release-session
call site, via `crawlpilot.identity.vault.Vault.save`), not here -- by the time
the reaper destroys an IDLE context, its vault entry should already be
current, so destroy() itself has nothing further to checkpoint.

`self._registry` is typed against `RegistryProtocol`, not the concrete
in-memory `Registry`, so this reaper runs unmodified against P2's
`RedisRegistry` too -- same reasoning as `BrowserDriver` being a Protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import structlog

from crawlpilot import metrics
from crawlpilot.session.lease import is_expired
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState

log = structlog.get_logger(__name__)


def read_meminfo_used_pct() -> float | None:
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


def read_pid_rss_mb(pid: int) -> float | None:
    """Resident set size of one process, in MB, or `None` off Linux.

    Public, like its sibling `read_meminfo_used_pct` -- the leading underscore
    it carried was an oversight rather than a decision, and agentpilot's session
    listing was already importing it through that underscore. Either the name is
    part of what this module offers or the caller needs a different answer; it
    is the former, so it is spelled that way.
    """

    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    digits = "".join(ch for ch in line if ch.isdigit())
                    return int(digits) / 1024 if digits else None
    except OSError:
        return None
    return None


def read_tree_rss_mb(pid: int) -> float | None:
    """Resident set size of a process AND everything it spawned, in MB.

    A browser is not a process, it is a process tree, and measuring only the
    root is why the per-context ceiling never fired. Chrome puts each tab, the
    GPU process, the network service and every utility in its own child; the
    root holds comparatively little.

    MEASURED, inside a worker: the Chrome root was 292 MB while its children
    were a few MB each, so a 4096 MB ceiling read against the root alone could
    not be reached by any real page. The tree it belongs to was comfortably over
    a gigabyte. The node filled up, the reaper found nothing to say, and a tab
    died with `page_crash`.

    Walks `/proc/<pid>/task/*/children`, which is the kernel's own parent->child
    index -- no `ps`, no `psutil`, same discipline as `read_meminfo_used_pct`.
    Missing entries are skipped rather than failing the sum: a browser being
    torn down while this reads it is the ordinary case, not an error.
    """

    seen: set[int] = set()
    total = 0.0
    found_any = False
    stack = [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        rss = read_pid_rss_mb(current)
        if rss is not None:
            total += rss
            found_any = True
        stack.extend(_child_pids(current))
    return total if found_any else None


def _child_pids(pid: int) -> list[int]:
    """Direct children of `pid`, from the kernel rather than from `ps`.

    Every thread of a process has its own `children` file and they do not
    overlap, so all of them are read. A thread that exits mid-walk simply
    contributes nothing.
    """

    out: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return out
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/children") as f:
                out.extend(int(part) for part in f.read().split())
        except (OSError, ValueError):
            continue
    return out


class Reaper:
    def __init__(
        self,
        registry: RegistryProtocol,
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
        now_monotonic_entries = await self._registry.snapshot()
        for identity, ctx, lease, _released_at in now_monotonic_entries:
            if lease is None or ctx.state is not ContextState.ACTIVE:
                continue
            if is_expired(lease):
                log.warning("reaper.lease_expired", identity=identity.slug())
                metrics.incr("reaper_lease_reclaimed_total")
                await self._registry.force_release(identity)

    async def _enforce_per_process_ceiling(self) -> None:
        for identity, ctx, _lease, _released_at in await self._registry.snapshot():
            if ctx.pid is None:
                continue
            # The whole browser, not just its root process. See
            # `read_tree_rss_mb` for why the root alone made this unreachable.
            rss = read_tree_rss_mb(ctx.pid)
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
        for identity, ctx, _lease, released_at in await self._registry.snapshot():
            if ctx.state is not ContextState.IDLE or released_at is None:
                continue
            if now - released_at >= self.idle_ttl_seconds:
                await self._destroy(identity, ctx, reason="idle_ttl")

    async def _reap_memory_pressure(self) -> None:
        used_pct = read_meminfo_used_pct()
        if used_pct is None or used_pct < self.mem_pressure_watermark_pct:
            return

        idle_oldest_first = sorted(
            (
                (identity, ctx, released_at)
                for identity, ctx, _lease, released_at in await self._registry.snapshot()
                if ctx.state is ContextState.IDLE and released_at is not None
            ),
            key=lambda row: row[2],
        )
        for identity, ctx, _released_at in idle_oldest_first:
            used_pct = read_meminfo_used_pct()
            if used_pct is None or used_pct < self.mem_pressure_watermark_pct:
                break
            log.warning(
                "reaper.memory_pressure_evict", identity=identity.slug(), used_pct=used_pct
            )
            await self._destroy(identity, ctx, reason="memory_pressure")

    async def _destroy(self, identity: IdentityRef, ctx: ContextRef, *, reason: str) -> None:
        # Vault save-on-release-to-IDLE is stubbed until P2's vault.py lands
        # (plan.md: profile dirs are a node-local cache, vault is source of
        # truth) -- P1 destroys straight from the profile-dir cache, so a
        # destroy here loses warmth but not correctness (nothing durable
        # exists yet to lose).
        evicted = await self._registry.evict(identity)
        if evicted is None:
            return
        metrics.incr("reaper_destroyed_total", reason=reason)
        await self._driver.close(evicted)
