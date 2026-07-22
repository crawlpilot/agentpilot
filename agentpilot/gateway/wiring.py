"""The composition root -- the ONLY file in the repo that imports `agentpilot.driver`.

Role-aware (see `agentpilot.gateway.role`): a `worker`/`monolith` process owns the
shared Patchright singleton, the `PatchrightDriver`, the registry (Redis-
backed when `AGENTPILOT_REDIS_URL` is set, in-memory otherwise -- see
`agentpilot.session.registry.RegistryProtocol`), the `Reaper`, and P2's identity
layer (`Vault`/`ProxyPinner`, both optional). A `gateway` process constructs
none of that -- just an httpx client and a Redis client for the
`session_id -> worker` routing table, and never touches `agentpilot.driver`.

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

from agentpilot.auth.store import ApiKeyStoreProtocol, InMemoryApiKeyStore, PostgresApiKeyStore
from agentpilot.gateway.role import Role, get_role
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, LeaseId
from agentpilot.spi.proxy import ProxyEndpoint


@dataclass
class Session:
    session_id: str
    identity: IdentityKey
    ctx: ContextRef
    lease_id: LeaseId
    tier: str
    headful: bool
    block_popups: bool
    enable_cdp: bool


def _parse_proxy_pool(raw: str) -> list[ProxyEndpoint]:
    """`AGENTPILOT_PROXY_POOL` format: comma-separated `scheme://[user:pass@]host:port`."""

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
        self.lease_ttl_seconds = float(os.environ.get("AGENTPILOT_LEASE_TTL_SECONDS", "300"))
        self.cdp_max_connection_seconds = float(
            os.environ.get("AGENTPILOT_CDP_MAX_CONNECTION_SECONDS", "14400")
        )

        redis_url = os.environ.get("AGENTPILOT_REDIS_URL")
        self.redis: Redis | None = Redis.from_url(redis_url) if redis_url else None

        # Placeholder until `_connect_api_keys()` (awaited from `get_wiring()`
        # below) replaces it for `monolith`/`gateway` when `AGENTPILOT_DATABASE_URL`
        # is set. `worker` never mounts an auth-gated route and never touches
        # `wiring.api_keys` at all -- leaving this as `InMemoryApiKeyStore`
        # for it is correct, not merely cheap, now that opening the real
        # backend is an async network round-trip rather than Redis's lazy
        # client.
        self.api_keys: ApiKeyStoreProtocol = InMemoryApiKeyStore()
        self._database_url = os.environ.get("AGENTPILOT_DATABASE_URL")
        self.admin_token = os.environ.get("AGENTPILOT_ADMIN_TOKEN")

        if self.role == "gateway":
            self._init_gateway()
        else:
            self._init_worker()

    async def _connect_api_keys(self) -> None:
        """Only `monolith`/`gateway` mount tenant-facing auth-gated routes, so
        only they ever get a real (Postgres-backed) store -- `worker` keeps
        the `InMemoryApiKeyStore()` placeholder from `__init__` forever,
        since it would never be queried anyway. Split out of `__init__`
        because `AsyncConnectionPool.open()` must be awaited (unlike Redis's
        lazy `from_url()`), which can only happen inside a coroutine -- see
        `get_wiring()`'s docstring for why that's where this gets called."""

        if self.role not in ("monolith", "gateway"):
            return
        if self._database_url:
            self.api_keys = await PostgresApiKeyStore.connect(self._database_url)
        # else: AGENTPILOT_DATABASE_URL unset -> keep the InMemoryApiKeyStore()
        # placeholder (dev/test convenience only -- keys won't survive a
        # restart or be visible to any other process).

    def _init_gateway(self) -> None:
        # Gateway-role graph excludes agentpilot.driver (plan.md) -- these imports
        # are deferred into this method (not hoisted to module level) purely
        # so a gateway-role process's *call stack* never touches driver code,
        # even though the module-level import above this class still exists
        # (wiring.py is the composition root, exempt from that contract
        # either way). `docker/gateway.Dockerfile` is a genuinely driver-free
        # image (no Chrome/Xvfb/Patchright at all) -- see `agentpilot/gateway/
        # role.py`'s docstring.
        if self.redis is None:
            raise RuntimeError("AGENTPILOT_ROLE=gateway requires AGENTPILOT_REDIS_URL")
        self.worker_base_url = os.environ.get("AGENTPILOT_WORKER_URL", "http://worker:8000")
        self.http_client = httpx.AsyncClient(timeout=90.0)

    def _init_worker(self) -> None:
        from agentpilot.driver.patchright_driver import PatchrightDriver
        from agentpilot.driver.process_launcher import ProcessLauncher
        from agentpilot.identity.vault import Vault
        from agentpilot.session.reaper import Reaper
        from agentpilot.session.redis_registry import RedisRegistry
        from agentpilot.session.registry import Registry, RegistryProtocol
        from agentpilot.spi.driver import BrowserDriver

        self.launcher = ProcessLauncher()
        self.driver: BrowserDriver = PatchrightDriver(
            self.launcher,
            max_tabs_per_session=int(os.environ.get("AGENTPILOT_MAX_TABS_PER_SESSION", "10")),
        )
        assert isinstance(self.driver, BrowserDriver)

        # Loopback-only fetches (a session's own local CDP `/json/version`)
        # -- fail fast, not the gateway's 90s cross-network timeout.
        self.cdp_http_client = httpx.AsyncClient(timeout=5.0)

        self.profiles_root = Path(
            os.environ.get("AGENTPILOT_PROFILES_DIR", "/var/lib/agentpilot/profiles")
        )

        self.registry: RegistryProtocol
        self.registry = RedisRegistry(self.redis) if self.redis is not None else Registry()

        self.reaper = Reaper(
            self.registry,
            self.driver,
            idle_ttl_seconds=float(os.environ.get("AGENTPILOT_IDLE_TTL_SECONDS", "300")),
            scan_interval_seconds=float(
                os.environ.get("AGENTPILOT_REAPER_INTERVAL_SECONDS", "15")
            ),
            mem_pressure_watermark_pct=float(
                os.environ.get("AGENTPILOT_MEM_WATERMARK_PCT", "85")
            ),
            per_process_ceiling_mb=float(
                os.environ.get("AGENTPILOT_PER_PROCESS_CEILING_MB", "4096")
            ),
        )
        self.reaper.start()

        self.vault: Vault | None = None
        vault_key = os.environ.get("AGENTPILOT_VAULT_KEY")
        if vault_key:
            vault_root = Path(os.environ.get("AGENTPILOT_VAULT_DIR", "/var/lib/agentpilot/vault"))
            self.vault = Vault(vault_root, vault_key.encode())

        self.proxy_pinner: ProxyPinner | None = None
        proxy_pool_raw = os.environ.get("AGENTPILOT_PROXY_POOL")
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
            await self.cdp_http_client.aclose()
        if isinstance(self.api_keys, PostgresApiKeyStore):
            await self.api_keys.close()
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
    `create_task` has one. `_connect_api_keys()` is awaited here for the
    same reason: opening a Postgres pool needs a real `await`, which neither
    a plain-`def` `Depends()` callable nor `Wiring.__init__` itself (kept
    synchronous) can do.
    """

    global _wiring
    if _wiring is None:
        wiring = Wiring()
        try:
            await wiring._connect_api_keys()
        except BaseException:
            # Don't leave an orphaned reaper task / launcher / pool behind
            # from this failed attempt -- clean up so the *next* request
            # gets a genuinely fresh Wiring() rather than leaking a new one
            # on every request while Postgres is unreachable.
            await wiring.close()
            raise
        _wiring = wiring
    return _wiring


async def reset_wiring() -> None:
    """Test-only: tears down and clears the singleton so each test gets a
    fresh driver/session store."""

    global _wiring
    if _wiring is not None:
        await _wiring.close()
    _wiring = None
