"""FastAPI app: wires routers + exception handlers, role-aware (see
`agentpilot.gateway.role`). Zero session state lives here -- that's
`wiring.Wiring`, constructed lazily via `get_wiring()`.

- `monolith` (default): everything -- `/v1/sessions` (real logic, tenant-
  auth-gated), `/internal/sessions` (the same routes, also reachable,
  trusted-network, no auth gate), live view on both, `/v1/scrape` +
  `/internal/scrape` (`routes/scrape.py`, same dual-mount idiom), `/v1/map`
  (`routes/map.py`, driver-free discovery, no `/internal/...` counterpart
  needed), `/v1/crawl` (`routes/crawl.py`, job CRUD against `agentpilot.jobs
  .store`, also no `/internal/...` counterpart -- the actual crawl
  *processing* is `wiring.crawl_worker_loop`, a background task, not an
  HTTP route at all), `/v1/agent/runs` (`routes/agent_runs.py`, same shape
  again against `agentpilot.jobs.agent_store` -- processing is
  `wiring.agent_worker_loop`), and `/v1/api-keys` (admin-gated). Unchanged
  P0/P1 behavior plus P2's auth additions and the P4/agent-platform endpoints.
- `worker`: only `/internal/sessions` and `/internal/scrape` (real logic,
  including live view as of this pass) -- never `/v1/...`, per `plan.md`'s
  "internal-only, never tenant-exposed" topology. `/v1/map`/`/v1/crawl` are
  never mounted here either -- worker's contribution to crawling is
  `wiring.crawl_worker_loop` (`_init_worker()`), not an HTTP surface.
- `gateway`: `/v1/sessions` as a proxy to a worker (`routes/gateway_proxy.py`,
  auth-gated per-route since this router only ever serves this one mount),
  `/v1/sessions/{id}/live-view` as a WS relay (`routes/live_view_proxy.py`),
  `/v1/scrape` as a proxy (`routes/scrape_proxy.py`, same per-route auth
  idiom), `/v1/map` and `/v1/crawl` (the *same* `routes/map.py`/`routes/
  crawl.py` routers as monolith -- no proxy needed, since neither touches
  `agentpilot.driver`), and `/v1/api-keys` (admin-gated). Never
  imports/constructs a driver.

The frontend (`frontend/`) is normally deployed separately (dev server
locally, a static host in production) -- there's no Node/npm in any of this
repo's Docker images, exactly how Firecrawl keeps its own dashboard
(`apps/ui/ingestion-ui`) out of its backend images. As an opt-in convenience
the built SPA can instead be served *same-origin* from the tenant-facing
roles (`monolith`/`gateway`) so no reverse proxy is needed -- see
`agentpilot.gateway.spa`; it stays inert (nothing mounted) unless a build is
present, and `worker` never serves it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from agentpilot.gateway.auth_deps import require_admin, require_tenant_auth
from agentpilot.gateway.errors import register_exception_handlers
from agentpilot.gateway.role import get_role
from agentpilot.gateway.routes import (
    agent_runs,
    api_keys,
    cdp,
    cdp_proxy,
    crawl,
    gateway_proxy,
    health,
    live_view,
    live_view_proxy,
    nodes,
    recipes,
    scrape,
    scrape_proxy,
    sessions,
)
from agentpilot.gateway.routes import map as map_routes
from agentpilot.gateway.spa import mount_spa, resolve_ui_dir
from agentpilot.gateway.wiring import get_wiring, reset_wiring


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # `get_wiring()` is otherwise built lazily on the first request that
    # `Depends()`s it. For a `worker`, that first-request trigger is exactly
    # what starts `NodeRegistry`'s self-registration heartbeat -- but a
    # worker is internal-only (never gets a stray tenant request) and
    # nothing else pings it, so without this, a freshly booted worker never
    # writes itself into `live_nodes` and every placement 503s with
    # CAPACITY_EXHAUSTED until *something* happens to hit it. Eager-init at
    # startup instead, for every role, matching this codebase's existing
    # fail-fast-at-boot convention (e.g. AGENTPILOT_NODE_ADDR's check).
    await get_wiring()
    yield
    await reset_wiring()


app = FastAPI(title="agentpilot", version="0.1.0", lifespan=_lifespan)
register_exception_handlers(app)
app.include_router(health.router)

_role = get_role()

if _role in ("monolith", "worker"):
    app.include_router(sessions.router, prefix="/internal/sessions")
    app.include_router(live_view.router, prefix="/internal/sessions")
    app.include_router(cdp.router, prefix="/internal/sessions")
    app.include_router(scrape.router, prefix="/internal/scrape")
if _role == "monolith":
    app.include_router(
        sessions.router, prefix="/v1/sessions", dependencies=[Depends(require_tenant_auth)]
    )
    app.include_router(live_view.router, prefix="/v1/sessions")
    app.include_router(cdp.router, prefix="/v1/sessions")
    app.include_router(
        scrape.router, prefix="/v1/scrape", dependencies=[Depends(require_tenant_auth)]
    )
    # No router-level dependency: map_routes.router's one route already
    # declares `Depends(require_tenant_auth)` itself (the only way to
    # actually receive the resolved `AuthedTenant` in the handler).
    app.include_router(map_routes.router, prefix="/v1/map")
    # Same reasoning as map_routes.router: every crawl.router route already
    # declares Depends(require_tenant_auth) itself. Job CRUD never touches
    # agentpilot.driver, so this needs no /internal/... counterpart either.
    app.include_router(crawl.router, prefix="/v1/crawl")
    # Same reasoning again -- agent_runs.router's routes each declare their
    # own Depends(require_tenant_auth); run CRUD never touches
    # agentpilot.driver either (agentpilot.jobs.agent_worker_loop does the
    # actual browsing, as a background task, not through this route at all).
    app.include_router(agent_runs.router, prefix="/v1/agent/runs")
    # Same reasoning again -- recipes.router's routes each declare their own
    # Depends(require_tenant_auth); run CRUD never touches agentpilot.driver
    # either (agentpilot.jobs.recipe_worker_loop does the actual browsing).
    app.include_router(recipes.router, prefix="/v1/recipes")
    app.include_router(
        api_keys.router, prefix="/v1/api-keys", dependencies=[Depends(require_admin)]
    )
if _role == "gateway":
    app.include_router(gateway_proxy.router, prefix="/v1/sessions")
    app.include_router(live_view_proxy.router, prefix="/v1/sessions")
    # No router-level dependency here (same reasoning as gateway_proxy.router
    # just above): scrape_proxy.router's one route already declares
    # `Depends(require_tenant_auth)` per-route.
    app.include_router(scrape_proxy.router, prefix="/v1/scrape")
    # No router-level `require_tenant_auth` dependency here (unlike
    # `gateway_proxy.router`): this router mixes a plain HTTP route (which
    # already declares `Depends(require_tenant_auth)` per-route, exactly
    # like `gateway_proxy.py`'s routes) with a WebSocket route, which can't
    # carry an `Authorization` header on its handshake and validates via its
    # own `?api_key=` check instead -- see `routes/cdp_proxy.py`. A
    # router-level dependency would apply to both and break the WS route.
    app.include_router(cdp_proxy.router, prefix="/v1/sessions")
    # Same router, same route, as the monolith mount above -- agentpilot.crawl
    # needs no worker hop, so gateway serves /v1/map out of its own process
    # directly rather than through a _proxy module.
    app.include_router(map_routes.router, prefix="/v1/map")
    # Same router as the monolith mount above, for the same reason -- job
    # CRUD (agentpilot.jobs.store) needs no worker hop either.
    app.include_router(crawl.router, prefix="/v1/crawl")
    # Same router as the monolith mount above, same reason.
    app.include_router(agent_runs.router, prefix="/v1/agent/runs")
    # Same router as the monolith mount above, same reason.
    app.include_router(recipes.router, prefix="/v1/recipes")
    app.include_router(
        api_keys.router, prefix="/v1/api-keys", dependencies=[Depends(require_admin)]
    )
    app.include_router(nodes.router, prefix="/v1/nodes", dependencies=[Depends(require_admin)])

# Optional same-origin SPA serving (see agentpilot.gateway.spa). Mounted last
# so every API router is matched first; only the tenant-facing roles serve it,
# and only when a built bundle is actually present.
if _role in ("monolith", "gateway"):
    _ui_dir = resolve_ui_dir()
    if _ui_dir is not None:
        mount_spa(app, _ui_dir)
