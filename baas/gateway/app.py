"""FastAPI app: wires routers + exception handlers, role-aware (see
`baas.gateway.role`). Zero session state lives here -- that's
`wiring.Wiring`, constructed lazily via `get_wiring()`.

- `monolith` (default): everything -- `/v1/sessions` (real logic),
  `/internal/sessions` (the same routes, also reachable, harmless), live
  view, and the `/ui` frontend. Unchanged P0/P1 behavior.
- `worker`: only `/internal/sessions` (real logic) -- never `/v1/...`,
  per `plan.md`'s "internal-only, never tenant-exposed" topology. Live view
  and `/ui` are a `monolith`-only convenience in this pass (see
  `routes/gateway_proxy.py`'s docstring on the live-view gap).
- `gateway`: only `/v1/sessions` as a proxy to a worker (`routes/
  gateway_proxy.py`) -- never imports/constructs a driver.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from baas.gateway.errors import register_exception_handlers
from baas.gateway.role import get_role
from baas.gateway.routes import gateway_proxy, health, live_view, sessions
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
if _role == "monolith":
    app.include_router(sessions.router, prefix="/v1/sessions")
    app.include_router(live_view.router)
    if _FRONTEND_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="ui")
if _role == "gateway":
    app.include_router(gateway_proxy.router, prefix="/v1/sessions")
