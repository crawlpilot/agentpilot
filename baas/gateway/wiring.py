"""The composition root -- the ONLY file in the repo that imports `baas.driver`.

Role-aware (see `baas.gateway.role`): a `worker`/`monolith` process owns the
shared Patchright singleton, the `PatchrightDriver`, the registry (Redis-
backed when `BAAS_REDIS_URL` is set, in-memory otherwise -- see
`baas.session.registry.RegistryProtocol`), the `Reaper`, and P2's identity
layer (`Vault`/`ProxyPinner`, both optional). A `gateway` process constructs
none of that -- just an httpx client and a Redis client for the
`session_id -> worker` routing table, and never touches `baas.driver`.

The `session_id -> Session` dict is worker/monolith-local: `Registry` is
keyed by `IdentityKey`, not `session_id`, since one warm context can be
reused across many session_ids over its lifetime (open, release, reopen
mints a new session_id for the same underlying context).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from redis.asyncio import Redis

from baas.gateway.role import Role, get_role
from baas.identity.proxy_pinning import ProxyPinner
from baas.identity.vault import Vault
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, LeaseId
from baas.spi.proxy import ProxyEndpoint


@dataclass
class Session:
    session_id: str
    identity: IdentityKey
    ctx: ContextRef
    lease_id: LeaseId
    tier: str
    headful: bool
    block_popups: bool


def _parse_proxy_pool(raw: str) -> list[ProxyEndpoint]:
    """`BAAS_PROXY_POOL` format: comma-separated `scheme://[user:pass@]host:port`."""

    pool = []
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        url = httpx.URL(entry)
        pool.append(
            ProxyEndpoint(
                scheme=url.scheme,
                host=url.host,
                port=url.port or (443 if url.scheme == "https" else 80),
                username=url.username or None,
                password=url.password or None,
            )
        )
    return pool


class Wiring:
    def __init__(self) -> None:
        self.role: Role = get_role()
        self.sessions: dict[str, Session] = {}
        self.lease_ttl_seconds = float(os.environ.get("BAAS_LEASE_TTL_SECONDS", "300"))

        redis_url = os.environ.get("BAAS_REDIS_URL")
        self.redis: Redis | None = Redis.from_url(redis_url) if redis_url else None

        if self.role == "gateway":
            self._init_gateway()
        else:
            self._init_worker()

    def _init_gateway(self) -> None:
        # Gateway-role graph excludes baas.driver (plan.md) -- these imports
        # are deferred into this method (not hoisted to module level) purely
        # so a gateway-role process's *call stack* never touches driver code,
        # even though the module-level import above this class still exists
        # (wiring.py is the composition root, exempt from that contract
        # either way -- see baas/gateway/role.py's docstring on why a truly
        # driver-free gateway image needs a separate Dockerfile this pass
        # doesn't build).
        if self.redis is None:
            raise RuntimeError("BAAS_ROLE=gateway requires BAAS_REDIS_URL")
        self.worker_base_url = os.environ.get("BAAS_WORKER_URL", "http://worker:8000")
        self.http_client = httpx.AsyncClient(timeout=90.0)

    def _init_worker(self) -> None:
        from baas.driver.patchright_driver import PatchrightDriver
        from baas.driver.process_launcher import ProcessLauncher
        from baas.session.reaper import Reaper
        from baas.session.redis_registry import RedisRegistry
        from baas.session.registry import Registry, RegistryProtocol
        from baas.spi.driver import BrowserDriver

        self.launcher = ProcessLauncher()
        self.driver: BrowserDriver = PatchrightDriver(self.launcher)
        assert isinstance(self.driver, BrowserDriver)

        self.profiles_root = Path(os.environ.get("BAAS_PROFILES_DIR", "/var/lib/baas/profiles"))

        self.registry: RegistryProtocol
        self.registry = RedisRegistry(self.redis) if self.redis is not None else Registry()

        self.reaper = Reaper(
            self.registry,
            self.driver,
            idle_ttl_seconds=float(os.environ.get("BAAS_IDLE_TTL_SECONDS", "300")),
            scan_interval_seconds=float(os.environ.get("BAAS_REAPER_INTERVAL_SECONDS", "15")),
            mem_pressure_watermark_pct=float(os.environ.get("BAAS_MEM_WATERMARK_PCT", "85")),
            per_process_ceiling_mb=float(os.environ.get("BAAS_PER_PROCESS_CEILING_MB", "4096")),
        )
        self.reaper.start()

        self.vault: Vault | None = None
        vault_key = os.environ.get("BAAS_VAULT_KEY")
        if vault_key:
            vault_root = Path(os.environ.get("BAAS_VAULT_DIR", "/var/lib/baas/vault"))
            self.vault = Vault(vault_root, vault_key.encode())

        self.proxy_pinner: ProxyPinner | None = None
        proxy_pool_raw = os.environ.get("BAAS_PROXY_POOL")
        if proxy_pool_raw and self.redis is not None:
            self.proxy_pinner = ProxyPinner(self.redis, _parse_proxy_pool(proxy_pool_raw))

    async def close(self) -> None:
        if self.role == "gateway":
            await self.http_client.aclose()
        else:
            await self.reaper.stop()
            for session in list(self.sessions.values()):
                await self.driver.close(session.ctx)
            await self.launcher.close()
        if self.redis is not None:
            await self.redis.aclose()


_wiring: Wiring | None = None


async def get_wiring() -> Wiring:
    """`async def`, not plain `def`: FastAPI/Starlette runs sync `Depends()`
    callables in a worker thread (`anyio.to_thread`), which has no running
    asyncio event loop of its own. `Wiring.__init__` (worker/monolith roles)
    starts the reaper via `asyncio.create_task`, which needs one -- a
    plain-`def` version of this crashed every request with `RuntimeError: no
    running event loop` the first time it constructed a `Wiring` off-thread.
    `async def` dependencies run directly on the event loop instead, so
    `create_task` has one."""

    global _wiring
    if _wiring is None:
        _wiring = Wiring()
    return _wiring


async def reset_wiring() -> None:
    """Test-only: tears down and clears the singleton so each test gets a
    fresh driver/session store."""

    global _wiring
    if _wiring is not None:
        await _wiring.close()
    _wiring = None
