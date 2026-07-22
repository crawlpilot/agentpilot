"""FastAPI app: wires routers + exception handlers. Zero session state lives
here -- that's `wiring.Wiring`, constructed lazily via `get_wiring()`."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from baas.gateway.errors import register_exception_handlers
from baas.gateway.routes import health, live_view, sessions
from baas.gateway.wiring import reset_wiring

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await reset_wiring()


app = FastAPI(title="baas-crawlpilot", version="0.1.0", lifespan=_lifespan)
register_exception_handlers(app)
app.include_router(sessions.router)
app.include_router(health.router)
app.include_router(live_view.router)

if _FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="ui")
