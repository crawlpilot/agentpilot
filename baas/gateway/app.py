"""FastAPI app: wires routers + exception handlers, role-aware (see
`baas.gateway.role`). Zero session state lives here -- that's
`wiring.Wiring`, constructed lazily via `get_wiring()`.

- `monolith` (default): everything -- `/v1/sessions` (real logic, tenant-
  auth-gated), `/internal/sessions` (the same routes, also reachable,
  trusted-network, no auth gate), live view on both, `/v1/api-keys`
  (admin-gated), and the `/ui` frontend. Unchanged P0/P1 behavior plus P2's
  auth additions.
- `worker`: only `/internal/sessions` (real logic, including live view as of
  this pass) -- never `/v1/...`, per `plan.md`'s "internal-only, never
  tenant-exposed" topology.
- `gateway`: `/v1/sessions` as a proxy to a worker (`routes/gateway_proxy.py`,
  auth-gated per-route since this router only ever serves this one mount),
  `/v1/sessions/{id}/live-view` as a WS relay (`routes/live_view_proxy.py`),
  `/v1/api-keys` (admin-gated), and `/ui` -- same-origin static hosting
  sidesteps CORS/WS-Origin config entirely for the SPA talking to this same
  process. Never imports/constructs a driver.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from baas.gateway.auth_deps import require_admin, require_tenant_auth
from baas.gateway.errors import register_exception_handlers
from baas.gateway.role import get_role
from baas.gateway.routes import (
    api_keys,
    gateway_proxy,
    health,
    live_view,
    live_view_proxy,
    sessions,
)
from baas.gateway.wiring import reset_wiring

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await reset_wiring()


app = FastAPI(title="baas-crawlpilot", version="0.1.0", lifespan=_lifespan)
register_exception_handlers(app)
app.include_router(health.router)

_role = get_role()

if _role in ("monolith", "worker"):
    app.include_router(sessions.router, prefix="/internal/sessions")
    app.include_router(live_view.router, prefix="/internal/sessions")
if _role == "monolith":
    app.include_router(
        sessions.router, prefix="/v1/sessions", dependencies=[Depends(require_tenant_auth)]
    )
    app.include_router(live_view.router, prefix="/v1/sessions")
    app.include_router(
        api_keys.router, prefix="/v1/api-keys", dependencies=[Depends(require_admin)]
    )
    if _FRONTEND_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="ui")
if _role == "gateway":
    app.include_router(gateway_proxy.router, prefix="/v1/sessions")
    app.include_router(live_view_proxy.router, prefix="/v1/sessions")
    app.include_router(
        api_keys.router, prefix="/v1/api-keys", dependencies=[Depends(require_admin)]
    )
    if _FRONTEND_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="ui")
