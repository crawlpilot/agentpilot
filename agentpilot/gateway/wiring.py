"""The composition root -- the ONLY file in the repo that imports `agentpilot.driver`.

Role-aware (see `agentpilot.gateway.role`): a `worker`/`monolith` process owns the
shared Patchright singleton, the `PatchrightDriver`, the registry (Redis-
backed when `AGENTPILOT_REDIS_URL` is set, in-memory otherwise -- see
`agentpilot.session.registry.RegistryProtocol`), the `Reaper`, and P2's identity
layer (`Vault`/`ProxyPinner`, both optional). A `worker` (not `monolith` --
see `_init_worker()`) additionally self-registers into the fleet via
`agentpilot.placement.node_registry.NodeRegistry`, so the gateway's
`SessionPlacer`/`NodeReaper` can see it. A `gateway` process constructs none
of the driver-side state -- just an httpx client, a Redis client, and the
placement layer (`SessionPlacer`, `NodeReaper`, a `RedisRegistry` used only
for the reaper's lease-eviction calls) -- and never touches `agentpilot.driver`.

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

    async def _register_node(self) -> None:
        """`NodeRegistry.register()` (the `node:{id}` HSET) needs a real
        `await`, same reason `_connect_api_keys()` is split out of the sync
        `__init__` -- see `get_wiring()`'s docstring. Starting the heartbeat
        loop itself (`start()`, sync) happens right after: the loop's first
        tick is 2s away regardless, so `register()` landing first here isn't
        just timing luck.

        `self.node_registry` only exists on `worker`-role instances
        (`_init_worker()`) -- `gateway` never sets the attribute at all, so
        this must bail on role first, not just `None`-check, to avoid an
        `AttributeError` when `get_wiring()` calls this unconditionally."""

        if self.role != "worker":
            return
        if self.node_registry is not None:
            await self.node_registry.register()
            self.node_registry.start()

    def _init_gateway(self) -> None:
        # Gateway-role graph excludes agentpilot.driver (plan.md) -- these imports
        # are deferred into this method (not hoisted to module level) purely
        # so a gateway-role process's *call stack* never touches driver code,
        # even though the module-level import above this class still exists
        # (wiring.py is the composition root, exempt from that contract
        # either way). `docker/gateway.Dockerfile` is a genuinely driver-free
        # image (no Chrome/Xvfb/Patchright at all) -- see `agentpilot/gateway/
        # role.py`'s docstring.
        from agentpilot.placement.node_reaper import NodeReaper
        from agentpilot.placement.placer import SessionPlacer
        from agentpilot.session.redis_registry import RedisRegistry

        if self.redis is None:
            raise RuntimeError("AGENTPILOT_ROLE=gateway requires AGENTPILOT_REDIS_URL")
        self.http_client = httpx.AsyncClient(timeout=90.0)
        self.affinity_ttl_seconds = float(
            os.environ.get("AGENTPILOT_AFFINITY_TTL_SECONDS", "86400")
        )

        self.placer = SessionPlacer(self.redis)
        # A second RedisRegistry construction, gateway-side this time (the
        # worker-side one lives in _init_worker()) -- harmless: RedisRegistry
        # has no dependency on agentpilot.driver, and Redis dedupes
        # registered Lua scripts by SHA, so this isn't wasted work, just
        # used here only for the node-reaper's registry.evict() calls.
        self._gateway_registry = RedisRegistry(self.redis)
        self.node_reaper = NodeReaper(self.redis, self._gateway_registry)
        self.node_reaper.start()

    def _init_worker(self) -> None:
        import socket
        import uuid

        from agentpilot.driver.patchright_driver import PatchrightDriver
        from agentpilot.driver.process_launcher import ProcessLauncher
        from agentpilot.identity.vault import Vault
        from agentpilot.placement.node_registry import NodeRegistry
        from agentpilot.session.reaper import Reaper
        from agentpilot.session.redis_registry import RedisRegistry
        from agentpilot.session.registry import Registry, RegistryProtocol
        from agentpilot.spi.driver import BrowserDriver

        self.node_id = os.environ.get(
            "AGENTPILOT_NODE_ID", f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        )

        self.launcher = ProcessLauncher()
        self.driver: BrowserDriver = PatchrightDriver(
            self.launcher,
            max_tabs_per_session=int(os.environ.get("AGENTPILOT_MAX_TABS_PER_SESSION", "10")),
            node_id=self.node_id,
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

        # Fleet self-registration -- only a real `worker` (not `monolith`,
        # which never goes through the gateway's placement/routing at all,
        # serving `/v1/sessions/...` directly) and only when there's a
        # shared Redis for the gateway to actually see it in.
        self.node_registry: NodeRegistry | None = None
        if self.role == "worker" and self.redis is not None:
            node_addr = os.environ.get("AGENTPILOT_NODE_ADDR")
            if not node_addr:
                raise RuntimeError(
                    "AGENTPILOT_ROLE=worker with AGENTPILOT_REDIS_URL set requires "
                    "AGENTPILOT_NODE_ADDR (the address the gateway reaches this worker at)"
                )
            self.node_registry = NodeRegistry(
                self.redis,
                self.registry,
                node_id=self.node_id,
                addr=node_addr,
                max_contexts=int(os.environ.get("AGENTPILOT_MAX_CONTEXTS_PER_NODE", "25")),
            )

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
            await self.node_reaper.stop()
            await self.http_client.aclose()
        else:
            if self.node_registry is not None:
                # Stop registration before the reaper/driver so no new
                # placement can land on this node mid-teardown.
                await self.node_registry.stop()
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
    `create_task` has one. `_connect_api_keys()`/`_register_node()` are
    awaited here for the same reason: a Postgres pool open and the
    `node:{id}` HSET both need a real `await`, which neither a plain-`def`
    `Depends()` callable nor `Wiring.__init__` itself (kept synchronous) can do.
    """

    global _wiring
    if _wiring is None:
        wiring = Wiring()
        try:
            await wiring._connect_api_keys()
            await wiring._register_node()
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
