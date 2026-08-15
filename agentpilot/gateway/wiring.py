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

P4 adds `jobs_store` (a `PostgresJobStore`, connected for *every* role that
has `AGENTPILOT_DATABASE_URL` set -- monolith/gateway need it to serve
`/v1/crawl`/`/v1/batch/scrape`, worker/monolith also need it for
`crawl_worker_loop`) and `crawl_worker_loop` (a `CrawlWorkerLoop`, only on
worker/monolith, folded into the existing role rather than a separate one --
see `agentpilot.jobs.worker_loop`'s module docstring for that decision).
The agent platform adds the exact same pair for `/v1/agent/runs`: `agent_store`
(a `PostgresAgentStore`) and `agent_worker_loop` (an `AgentWorkerLoop`), same
connect/role rules as their crawl counterparts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from redis.asyncio import Redis

if TYPE_CHECKING:
    # Deferred (see _start_crawl_worker_loop()'s docstring): this keeps the
    # same "role-specific imports stay out of the module-level call stack"
    # discipline `_init_gateway()`'s own deferred imports already follow,
    # even though agentpilot.jobs.worker_loop doesn't touch agentpilot.driver
    # and so isn't *required* to be deferred by that contract specifically.
    from agentpilot.jobs.agent_worker_loop import AgentWorkerLoop
    from agentpilot.jobs.recipe_scheduler_loop import RecipeSchedulerLoop
    from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop
    from agentpilot.jobs.worker_loop import CrawlWorkerLoop

from agentpilot.auth.store import ApiKeyStoreProtocol, InMemoryApiKeyStore, PostgresApiKeyStore
from agentpilot.gateway.role import Role, get_role
from agentpilot.identity.burn_tracker import BurnTracker
from agentpilot.identity.proxy_config import ProxyConfig
from agentpilot.identity.proxy_health import ProxyHealth
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.jobs.agent_store import PostgresAgentStore
from agentpilot.jobs.recipe_store import PostgresRecipeStore
from agentpilot.jobs.store import PostgresJobStore
from agentpilot.session.interactive import InteractiveSession
from agentpilot.spi.proxy import ProxyEndpoint

# `Session` used to be defined here; it's now `agentpilot.session.interactive
# .InteractiveSession` (moved so `agentpilot.agent`'s step loop can open/drive
# a session too, without importing `agentpilot.gateway`). Re-exported under
# the old name for any external code still importing `gateway.wiring.Session`.
Session = InteractiveSession


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

        # Populated by _connect_jobs_store() (awaited from get_wiring(), same
        # reason _connect_api_keys() is split out of this sync __init__) --
        # every role can use it (monolith/gateway serve /v1/crawl directly;
        # worker/monolith also run CrawlWorkerLoop against it), unlike
        # api_keys there is no in-memory fallback: a durable job queue with
        # no durability isn't a meaningful stand-in, so routes/crawl.py
        # checks `wiring.jobs_store is not None` itself and returns a clear
        # error rather than silently degrading.
        self.jobs_store: PostgresJobStore | None = None
        self.agent_store: PostgresAgentStore | None = None
        """Same connect/role rules as `jobs_store` -- see `_connect_agent_store()`."""
        self.recipe_store: PostgresRecipeStore | None = None
        """Same connect/role rules as `jobs_store`/`agent_store` -- see
        `_connect_recipe_store()`."""

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

    async def _connect_jobs_store(self) -> None:
        """Unlike `_connect_api_keys()`, every role connects this if
        `AGENTPILOT_DATABASE_URL` is set -- monolith/gateway need it to serve
        `/v1/crawl`/`/v1/batch/scrape`, worker (and monolith again) needs it
        for `_start_crawl_worker_loop()`. No role-check, no in-memory
        fallback: see `jobs_store`'s own docstring in `__init__`."""

        if self._database_url:
            self.jobs_store = await PostgresJobStore.connect(self._database_url)

    async def _connect_agent_store(self) -> None:
        """Same rules as `_connect_jobs_store()`: monolith/gateway serve
        `/v1/agent/runs` directly, worker (and monolith again) needs it for
        `_start_agent_worker_loop()`."""

        if self._database_url:
            self.agent_store = await PostgresAgentStore.connect(self._database_url)

    async def _connect_recipe_store(self) -> None:
        """Same rules as `_connect_agent_store()`: monolith/gateway serve
        `/v1/recipes` directly, worker (and monolith again) needs it for
        `_start_recipe_worker_loop()`/`_start_recipe_scheduler_loop()`."""

        if self._database_url:
            self.recipe_store = await PostgresRecipeStore.connect(self._database_url)

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
        from agentpilot.session.warm_pool import KeepaliveLoop, WarmPool
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
        proxy_config = ProxyConfig.from_env()
        if not proxy_config.is_empty and self.redis is not None:
            # Health-aware: retire a proxy that has served too many pages (with
            # ±25% jitter) or lost too many connections, and skip retired ones
            # when picking / re-pin a warm identity off a retired exit.
            self.proxy_pinner = ProxyPinner(
                self.redis, proxy_config, ProxyHealth(self.redis)
            )

        # Per-identity burn accounting (retire a warm identity that keeps
        # getting walled). Redis-backed, so it survives restarts and is shared
        # across worker processes; None without Redis (dev/in-memory), which
        # simply disables burn tracking rather than failing.
        self.burn_tracker: BurnTracker | None = (
            BurnTracker(self.redis) if self.redis is not None else None
        )

        # Anticipatory warm-session pool -- pre-launched contexts per proxy
        # tier so a temporary scrape/agent-run skips the cold Chrome launch.
        # Inert at target 0 (the default), so this ships disabled and changes
        # nothing until an operator sets a per-tier target.
        warm_tiers: list[ProxyEndpoint | None] = (
            list(self.proxy_pinner.pool) if self.proxy_pinner is not None else [None]
        )
        self.warm_pool = WarmPool(
            self.driver,
            warm_tiers,
            profiles_root=self.profiles_root,
            target_per_tier=int(os.environ.get("AGENTPILOT_WARM_TARGET_PER_TIER", "0")),
            refill_interval_seconds=float(
                os.environ.get("AGENTPILOT_WARM_REFILL_INTERVAL_SECONDS", "5")
            ),
            mem_pressure_watermark_pct=float(os.environ.get("AGENTPILOT_MEM_WATERMARK_PCT", "85")),
        )
        self.warm_pool.start()

        # Keep idle warm contexts' CDP connections alive (proxies drop silent
        # idle sockets); evict/auto-restart any that stopped responding.
        self.keepalive_loop = KeepaliveLoop(
            self.registry,
            self.driver,
            warm_pool=self.warm_pool,
            interval_seconds=float(os.environ.get("AGENTPILOT_KEEPALIVE_INTERVAL_SECONDS", "30")),
        )
        self.keepalive_loop.start()

        # Placeholder until _start_crawl_worker_loop() (awaited from
        # get_wiring(), after _connect_jobs_store() populates jobs_store)
        # replaces it -- same two-step reason node_registry/api_keys need an
        # async follow-up after this sync __init__.
        self.crawl_worker_loop: CrawlWorkerLoop | None = None
        self.agent_worker_loop: AgentWorkerLoop | None = None
        """Same two-step placeholder-then-replace reason as `crawl_worker_loop`."""
        self.recipe_worker_loop: RecipeWorkerLoop | None = None
        self.recipe_scheduler_loop: RecipeSchedulerLoop | None = None
        """Same two-step placeholder-then-replace reason as `crawl_worker_loop`."""

    async def _start_crawl_worker_loop(self) -> None:
        """Only worker/monolith have a real local driver+registry to run
        ephemeral scrapes against -- gateway never constructs one at all
        (see `_init_gateway()`). A no-op if `AGENTPILOT_DATABASE_URL` was
        never set, matching `jobs_store`'s "no in-memory fallback" stance:
        crawl processing simply doesn't run without a durable queue behind
        it, rather than silently doing something wrong."""

        if self.role not in ("worker", "monolith") or self.jobs_store is None:
            return
        from agentpilot.jobs.worker_loop import CrawlWorkerLoop

        self.crawl_worker_loop = CrawlWorkerLoop(
            self.jobs_store,
            self.registry,
            self.driver,
            self.profiles_root,
            self.proxy_pinner,
            lease_ttl_seconds=self.lease_ttl_seconds,
            warm_pool=self.warm_pool,
        )
        self.crawl_worker_loop.start()

    async def _start_agent_worker_loop(self) -> None:
        """Same rules as `_start_crawl_worker_loop()`."""

        if self.role not in ("worker", "monolith") or self.agent_store is None:
            return
        from agentpilot.jobs.agent_worker_loop import AgentWorkerLoop
        from agentpilot.session.rotation import RotationConfig, RotationPolicy

        step_timeout_raw = os.environ.get("AGENTPILOT_AGENT_STEP_TIMEOUT_S")
        rotation = RotationConfig(
            enabled=os.environ.get("AGENTPILOT_ENABLE_CONTEXT_ROTATION", "").lower()
            in ("1", "true", "yes"),
            policy=RotationPolicy.parse(os.environ.get("AGENTPILOT_ROTATION_POLICY")),
        )
        self.agent_worker_loop = AgentWorkerLoop(
            self.agent_store,
            self.registry,
            self.driver,
            self.profiles_root,
            self.proxy_pinner,
            lease_ttl_seconds=self.lease_ttl_seconds,
            max_failures=int(os.environ.get("AGENTPILOT_AGENT_MAX_FAILURES", "5")),
            step_timeout_s=float(step_timeout_raw) if step_timeout_raw else None,
            enable_vision=os.environ.get("AGENTPILOT_AGENT_ENABLE_VISION", "").lower()
            in ("1", "true", "yes"),
            enable_judge=os.environ.get("AGENTPILOT_AGENT_ENABLE_JUDGE", "").lower()
            in ("1", "true", "yes"),
            rotation=rotation,
            # Live-view: register each run's session in the same in-process dict
            # routes/live_view.py resolves against, and (when this process has a
            # placer) publish the redis route so a gateway can proxy to it.
            sessions=self.sessions,
            placer=getattr(self, "placer", None),
        )
        self.agent_worker_loop.start()

    async def _start_recipe_worker_loop(self) -> None:
        """Same rules as `_start_agent_worker_loop()`."""

        if self.role not in ("worker", "monolith") or self.recipe_store is None:
            return
        from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop

        self.recipe_worker_loop = RecipeWorkerLoop(
            self.recipe_store,
            self.registry,
            self.driver,
            self.profiles_root,
            self.proxy_pinner,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        self.recipe_worker_loop.start()

    async def _start_recipe_scheduler_loop(self) -> None:
        """The minimal internal per-recipe scheduler -- needs no driver at
        all (it only enqueues `recipe_runs` rows), but stays folded into
        worker/monolith same as the other loops, for one consistent "who
        runs background loops" story."""

        if self.role not in ("worker", "monolith") or self.recipe_store is None:
            return
        from agentpilot.jobs.recipe_scheduler_loop import RecipeSchedulerLoop

        self.recipe_scheduler_loop = RecipeSchedulerLoop(self.recipe_store)
        self.recipe_scheduler_loop.start()

    async def close(self) -> None:
        if self.role == "gateway":
            await self.node_reaper.stop()
            await self.http_client.aclose()
        else:
            if self.node_registry is not None:
                # Stop registration before the reaper/driver so no new
                # placement can land on this node mid-teardown.
                await self.node_registry.stop()
            if self.crawl_worker_loop is not None:
                # Same reasoning, one step earlier: stop claiming/processing
                # tasks before the reaper/driver so nothing tries to open a
                # fresh ephemeral context on a driver that's about to close.
                await self.crawl_worker_loop.stop()
            if self.agent_worker_loop is not None:
                await self.agent_worker_loop.stop()
            if self.recipe_worker_loop is not None:
                await self.recipe_worker_loop.stop()
            if self.recipe_scheduler_loop is not None:
                await self.recipe_scheduler_loop.stop()
            await self.keepalive_loop.stop()
            await self.warm_pool.stop()
            await self.reaper.stop()
            for session in list(self.sessions.values()):
                await self.driver.close(session.ctx)
            await self.launcher.close()
            await self.cdp_http_client.aclose()
        if isinstance(self.api_keys, PostgresApiKeyStore):
            await self.api_keys.close()
        if self.jobs_store is not None:
            await self.jobs_store.close()
        if self.agent_store is not None:
            await self.agent_store.close()
        if self.recipe_store is not None:
            await self.recipe_store.close()
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
            await wiring._connect_jobs_store()
            await wiring._start_crawl_worker_loop()
            await wiring._connect_agent_store()
            await wiring._start_agent_worker_loop()
            await wiring._connect_recipe_store()
            await wiring._start_recipe_worker_loop()
            await wiring._start_recipe_scheduler_loop()
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
