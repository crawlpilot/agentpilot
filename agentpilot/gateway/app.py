"""FastAPI app: wires routers + exception handlers, role-aware (see
`agentpilot.gateway.role`). Zero session state lives here -- that's
`wiring.Wiring`, constructed lazily via `get_wiring()`.

- `monolith` (default): everything -- `/v1/sessions` (real logic, tenant-
  auth-gated), `/internal/sessions` (the same routes, also reachable,
  trusted-network, no auth gate), live view on both, and `/v1/api-keys`
  (admin-gated). Unchanged P0/P1 behavior plus P2's auth additions.
- `worker`: only `/internal/sessions` (real logic, including live view as of
  this pass) -- never `/v1/...`, per `plan.md`'s "internal-only, never
  tenant-exposed" topology.
- `gateway`: `/v1/sessions` as a proxy to a worker (`routes/gateway_proxy.py`,
  auth-gated per-route since this router only ever serves this one mount),
  `/v1/sessions/{id}/live-view` as a WS relay (`routes/live_view_proxy.py`),
  and `/v1/api-keys` (admin-gated). Never imports/constructs a driver.

The frontend (`frontend/`) is never served from here -- no `/ui` mount, no
Node/npm anywhere in this repo's Docker images. It's deployed completely
separately (dev server locally, a static host later), exactly how Firecrawl
keeps its own dashboard (`apps/ui/ingestion-ui`) out of its backend images
and docker-compose entirely.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from agentpilot.gateway.auth_deps import require_admin, require_tenant_auth
from agentpilot.gateway.errors import register_exception_handlers
from agentpilot.gateway.role import get_role
from agentpilot.gateway.routes import (
    api_keys,
    cdp,
    cdp_proxy,
    gateway_proxy,
    health,
    live_view,
    live_view_proxy,
    sessions,
)
from agentpilot.gateway.wiring import reset_wiring


@asynccontextmanager
async def _lifespan(app: FastAPI):
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
if _role == "monolith":
    app.include_router(
        sessions.router, prefix="/v1/sessions", dependencies=[Depends(require_tenant_auth)]
    )
    app.include_router(live_view.router, prefix="/v1/sessions")
    app.include_router(cdp.router, prefix="/v1/sessions")
    app.include_router(
        api_keys.router, prefix="/v1/api-keys", dependencies=[Depends(require_admin)]
    )
if _role == "gateway":
    app.include_router(gateway_proxy.router, prefix="/v1/sessions")
    app.include_router(live_view_proxy.router, prefix="/v1/sessions")
    # No router-level `require_tenant_auth` dependency here (unlike
    # `gateway_proxy.router`): this router mixes a plain HTTP route (which
    # already declares `Depends(require_tenant_auth)` per-route, exactly
    # like `gateway_proxy.py`'s routes) with a WebSocket route, which can't
    # carry an `Authorization` header on its handshake and validates via its
    # own `?api_key=` check instead -- see `routes/cdp_proxy.py`. A
    # router-level dependency would apply to both and break the WS route.
    app.include_router(cdp_proxy.router, prefix="/v1/sessions")
    app.include_router(
        api_keys.router, prefix="/v1/api-keys", dependencies=[Depends(require_admin)]
    )
