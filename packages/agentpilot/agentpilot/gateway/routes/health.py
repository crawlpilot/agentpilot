"""Liveness/readiness + `/metrics` -- every metric object lives in
`agentpilot.observability.metrics`; this route only samples current registry state
into the pool gauges on scrape (pull-model, not pushed on every mutation,
since a Prometheus scrape interval is the only consumer)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.observability.metrics import contexts_active, contexts_idle
from agentpilot.spi.lease import ContextState

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(wiring: Wiring = Depends(get_wiring)) -> dict[str, str]:
    return {"status": "ready"}


@router.get("/metrics")
async def metrics(wiring: Wiring = Depends(get_wiring)) -> Response:
    # A gateway-role Wiring has no registry at all (never opens contexts) --
    # request/error counters (already in the process-global registry) are
    # still meaningful there, just not the pool gauges.
    if wiring.role != "gateway":
        active = idle = 0
        for _identity, ctx, _lease, _released_at in await wiring.registry.snapshot():
            if ctx.state is ContextState.ACTIVE:
                active += 1
            elif ctx.state is ContextState.IDLE:
                idle += 1
        contexts_active.set(active)
        contexts_idle.set(idle)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
