"""Anticipatory warm-session pool -- pre-launched browser contexts, keyed by
proxy tier, so a temporary identity's first request skips the ~1-3s cold Chrome
launch. The BaaS analog of agent-browser's warm sandbox template + persistent
daemon (warm start ~1ms).

Why per proxy-tier (not per identity): `driver.open` binds proxy + profile at
launch, so a generic warm browser can't have a tenant's proxy retrofitted. But
`ProxyPinner` maps every identity deterministically to *one* endpoint in a fixed
pool, and temporary identities (ephemeral scrapes / agent runs) have no
persistent profile -- so a context pre-launched with a tier's proxy + a blank
pooled profile can be adopted by any temporary identity that hashes to that tier.
Permanent identities keep the registry's existing lazy per-identity warm reuse
and never touch this pool.

Integration is via the existing `opener` seam: a temporary identity's opener
calls `WarmPool.take(proxy)` first and cold-opens only on a miss. The pool owns
its contexts' lifecycle until `take`; after that the caller owns close, and the
pool sweeps the orphaned profile dir once the context is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path

import structlog

from agentpilot.identity.profile_store import delete_profile_dir, resolve_profile_dir
from agentpilot.session.reaper import read_meminfo_used_pct
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey, ProfileKind
from agentpilot.spi.lease import ContextRef
from agentpilot.spi.proxy import ProxyEndpoint

log = structlog.get_logger(__name__)

_WARM_TENANT = "_warm"
"""Reserved tenant for pooled contexts' throwaway identities -- keeps their
profile dirs namespaced under `profiles_root/_warm/...`, away from real tenants."""


def tier_key(proxy: ProxyEndpoint | None) -> Hashable:
    """The pool bucket a proxy belongs to -- identity minus the per-identity
    `sticky_key`, so two identities pinned to the same endpoint share warm
    contexts. `None` (direct/no-proxy) is its own tier."""

    if proxy is None:
        return None
    return (proxy.scheme, proxy.host, proxy.port, proxy.username)


@dataclass
class _Pooled:
    ctx: ContextRef
    identity: IdentityKey
    """The pooled context's throwaway identity -- carried only so its profile
    dir can be deleted via `delete_profile_dir` on destroy/sweep."""


class WarmPool:
    """Maintains up to `target_per_tier` ready contexts per proxy tier via a
    background refill loop (start/stop lifecycle mirrors `Reaper`). Inert when
    `target_per_tier == 0`, so shipping it disabled changes nothing."""

    def __init__(
        self,
        driver: BrowserDriver,
        tiers: list[ProxyEndpoint | None],
        *,
        profiles_root: Path,
        target_per_tier: int = 0,
        refill_interval_seconds: float = 5.0,
        mem_pressure_watermark_pct: float = 85.0,
    ) -> None:
        self._driver = driver
        # De-duplicate tiers by bucket; `None` (direct) is always a valid tier.
        self._tiers: list[ProxyEndpoint | None] = tiers or [None]
        self._profiles_root = profiles_root
        self.target_per_tier = target_per_tier
        self.refill_interval_seconds = refill_interval_seconds
        self.mem_pressure_watermark_pct = mem_pressure_watermark_pct
        self._ready: dict[Hashable, list[_Pooled]] = defaultdict(list)
        self._taken: dict[str, _Pooled] = {}
        self._task: asyncio.Task[None] | None = None

    # ----------------------------------------------------------------- acquire

    async def take(self, proxy: ProxyEndpoint | None) -> ContextRef | None:
        """Hand a ready context for `proxy`'s tier to a caller (who now owns
        close), or `None` when the tier is empty. Skips-and-destroys any pooled
        context that has died since it was warmed, so a caller never adopts a
        zombie."""

        bucket = self._ready.get(tier_key(proxy))
        while bucket:
            pooled = bucket.pop()
            if await self._driver.is_alive(pooled.ctx):
                self._taken[pooled.ctx.context_id] = pooled
                return pooled.ctx
            await self._destroy(pooled)
        return None

    def ready_count(self, proxy: ProxyEndpoint | None) -> int:
        return len(self._ready.get(tier_key(proxy), ()))

    # ------------------------------------------------------------- background

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.drain()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("warm_pool.tick_failed")
            await asyncio.sleep(self.refill_interval_seconds)

    async def tick(self) -> None:
        await self._sweep_taken()
        await self._prune_dead_ready()
        await self._refill()

    async def _refill(self) -> None:
        if self.target_per_tier <= 0:
            return
        used = read_meminfo_used_pct()
        if used is not None and used >= self.mem_pressure_watermark_pct:
            # Never warm speculatively while the node is already tight -- warm
            # contexts are a latency optimization, not worth OOM-risking live work.
            return
        for tier in self._tiers:
            bucket = self._ready[tier_key(tier)]
            while len(bucket) < self.target_per_tier:
                pooled = await self._open(tier)
                if pooled is None:
                    break  # open failed -> stop hammering this tier this tick
                bucket.append(pooled)

    async def keepalive_ready(self) -> None:
        """Ping every ready context so an idle proxy doesn't drop its CDP
        connection; drop and destroy any that has gone unresponsive. Driven by
        the shared keepalive loop (see `KeepaliveLoop`)."""

        for bucket in self._ready.values():
            survivors: list[_Pooled] = []
            for pooled in bucket:
                if await self._driver.keepalive(pooled.ctx):
                    survivors.append(pooled)
                else:
                    await self._destroy(pooled)
            bucket[:] = survivors

    async def _prune_dead_ready(self) -> None:
        for bucket in self._ready.values():
            survivors: list[_Pooled] = []
            for pooled in bucket:
                if await self._driver.is_alive(pooled.ctx):
                    survivors.append(pooled)
                else:
                    await self._destroy(pooled)
            bucket[:] = survivors

    async def _sweep_taken(self) -> None:
        """After a caller closes an adopted context, its pooled profile dir is
        orphaned. Once the context is no longer alive, delete the dir."""

        for context_id, pooled in list(self._taken.items()):
            if not await self._driver.is_alive(pooled.ctx):
                with contextlib.suppress(Exception):
                    delete_profile_dir(self._profiles_root, pooled.identity)
                del self._taken[context_id]

    async def _open(self, proxy: ProxyEndpoint | None) -> _Pooled | None:
        host = proxy.host if proxy is not None else "direct"
        identity = IdentityKey(
            tenant=_WARM_TENANT, domain=host, name=uuid.uuid4().hex, kind=ProfileKind.TEMPORARY
        )
        profile_dir = resolve_profile_dir(self._profiles_root, identity)
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            ctx = await self._driver.open(
                identity,
                profile_dir,
                proxy,
                headful=False,
                egress=EgressPolicy(),
                block_popups=True,
            )
        except Exception:
            log.warning("warm_pool.open_failed", tier=host)
            with contextlib.suppress(Exception):
                delete_profile_dir(self._profiles_root, identity)
            return None
        return _Pooled(ctx=ctx, identity=identity)

    async def _destroy(self, pooled: _Pooled) -> None:
        with contextlib.suppress(Exception):
            await self._driver.close(pooled.ctx)
        with contextlib.suppress(Exception):
            delete_profile_dir(self._profiles_root, pooled.identity)

    async def drain(self) -> None:
        """Destroy every ready context (on shutdown). Taken contexts belong to
        their callers; their profile dirs are swept once they're closed."""

        for bucket in self._ready.values():
            for pooled in bucket:
                await self._destroy(pooled)
        self._ready.clear()


class KeepaliveLoop:
    """Keeps otherwise-idle warm contexts' CDP connections alive so an
    intermediate proxy doesn't silently drop them (agent-browser's WS-ping /
    SO_KEEPALIVE analog). Pings IDLE registry contexts + the warm pool's ready
    contexts each tick; an unresponsive registry context is evicted so the next
    acquire auto-restarts a fresh one."""

    def __init__(
        self,
        registry: RegistryProtocol,
        driver: BrowserDriver,
        *,
        warm_pool: WarmPool | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self._registry = registry
        self._driver = driver
        self._warm_pool = warm_pool
        self.interval_seconds = interval_seconds
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
                await self.tick()
            except Exception:
                log.exception("keepalive.tick_failed")
            await asyncio.sleep(self.interval_seconds)

    async def tick(self) -> None:
        from agentpilot.spi.lease import ContextState

        for identity, ctx, _lease, _released_at in await self._registry.snapshot():
            if ctx.state is not ContextState.IDLE:
                continue
            if not await self._driver.keepalive(ctx):
                log.warning("keepalive.idle_context_dead", identity=identity.slug())
                with contextlib.suppress(Exception):
                    evicted = await self._registry.evict(identity)
                    if evicted is not None:
                        await self._driver.close(evicted)
        if self._warm_pool is not None:
            await self._warm_pool.keepalive_ready()
