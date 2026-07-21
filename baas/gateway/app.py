"""FastAPI app: wires routers + exception handlers. Zero session state lives
here -- that's `wiring.Wiring`, constructed lazily via `get_wiring()`."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from baas.gateway.errors import register_exception_handlers
from baas.gateway.routes import health, sessions
from baas.gateway.wiring import reset_wiring


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await reset_wiring()


app = FastAPI(title="baas-crawlpilot", version="0.1.0", lifespan=_lifespan)
register_exception_handlers(app)
app.include_router(sessions.router)
app.include_router(health.router)
