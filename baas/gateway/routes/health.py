"""Liveness/readiness + `/metrics` -- every metric object lives in
`baas.observability.metrics`; this route only samples current registry state
into the pool gauges on scrape (pull-model, not pushed on every mutation,
since a Prometheus scrape interval is the only consumer)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from baas.gateway.wiring import Wiring, get_wiring
from baas.observability.metrics import contexts_active, contexts_idle
from baas.spi.lease import ContextState

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(wiring: Wiring = Depends(get_wiring)) -> dict:
    return {"status": "ready"}


@router.get("/metrics")
async def metrics(wiring: Wiring = Depends(get_wiring)) -> Response:
    active = idle = 0
    for _identity, ctx, _lease, _released_at in await wiring.registry.snapshot():
        if ctx.state is ContextState.ACTIVE:
            active += 1
        elif ctx.state is ContextState.IDLE:
            idle += 1
    contexts_active.set(active)
    contexts_idle.set(idle)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
