"""Liveness/readiness + a minimal `/metrics` (pool-size gauge only; full
per-tier latency histograms land in P1's `baas.observability`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from baas.gateway.wiring import Wiring, get_wiring
from baas.spi.lease import ContextState

router = APIRouter(tags=["health"])

_contexts_active = Gauge("baas_contexts_active", "Number of ACTIVE browser contexts")
_contexts_idle = Gauge("baas_contexts_idle", "Number of IDLE browser contexts")


@router.get("/healthz")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(wiring: Wiring = Depends(get_wiring)) -> dict:
    return {"status": "ready"}


@router.get("/metrics")
async def metrics(wiring: Wiring = Depends(get_wiring)) -> Response:
    active = sum(1 for s in wiring.sessions.values() if s.ctx.state is ContextState.ACTIVE)
    idle = sum(1 for s in wiring.sessions.values() if s.ctx.state is ContextState.IDLE)
    _contexts_active.set(active)
    _contexts_idle.set(idle)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
