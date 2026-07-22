"""`gateway`-role's `/v1/sessions/...` -- a thin reverse proxy to a worker's
`/internal/sessions/...` (the exact same route implementations in
`routes/sessions.py`, just mounted under a different prefix on the worker).

Mounted **instead of** `routes/sessions.py` when `wiring.role == "gateway"`
(see `app.py`) -- a gateway process never imports/touches `baas.driver`.
Every route here requires `require_tenant_auth` (added at `include_router()`
time in `app.py`, not per-function, since this router is *only* ever mounted
at `/v1/sessions` -- there's no internal/trusted-mount ambiguity to handle the
way `routes/sessions.py` does): the gateway is the tenant-facing edge, so it's
the one place a real `tenant` has to come from an authenticated credential,
never a free-text request field. The authed tenant overwrites `req.tenant`
before forwarding, so the worker's internal surface can go on trusting its
caller completely -- the gateway is the only thing allowed to call it.

**Placement**: trivially "the one configured worker"
(`wiring.worker_base_url`) -- `plan.md`'s real placement (affinity,
capacity-weighted, node failure) needs more than one worker to place
across, which this pass doesn't have running. The routing-table mechanism
(`session:{id} -> worker_addr` in Redis, `baas.gateway.routing`) is still
real and would extend to multiple workers without a shape change; only the
*decision* is a stub.

Live-view WebSocket proxying now lives in `routes/live_view_proxy.py`
(previously a documented gap in this module -- see that file for the
bidirectional frame/input relay).
"""

from __future__ import annotations

import time

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from baas.auth.models import AuthedTenant
from baas.gateway.auth_deps import require_tenant_auth
from baas.gateway.routing import resolve_worker, session_route_key
from baas.gateway.schemas import SessionOpenRequest
from baas.gateway.wiring import Wiring, get_wiring
from baas.observability.metrics import requests_total, session_open_duration_seconds

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions-gateway-proxy"])


async def _proxy(
    wiring: Wiring, method: str, worker_url: str, path: str, **kwargs: object
) -> httpx.Response:
    url = f"{worker_url}{path}"
    try:
        return await wiring.http_client.request(method, url, **kwargs)  # type: ignore[arg-type]
    except httpx.TransportError as exc:
        log.error("gateway_proxy.worker_unreachable", url=url, error=str(exc))
        raise HTTPException(status_code=502, detail="worker unreachable") from exc


@router.post("")
async def open_session(
    req: SessionOpenRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="open_session").inc()
    started = time.monotonic()
    # Trivial single-worker "placement" -- see this module's docstring.
    worker_url = wiring.worker_base_url
    with session_open_duration_seconds.time():
        resp = await _proxy(wiring, "POST", worker_url, "/internal/sessions", json=req.model_dump())
    if resp.status_code == 200:
        session_id = resp.json()["session_id"]
        assert wiring.redis is not None
        await wiring.redis.set(session_route_key(session_id), worker_url)
    log.info("gateway_proxy.open", duration_ms=(time.monotonic() - started) * 1000)
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )


@router.get("")
async def list_sessions(
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    # Trivial single-worker fan-out -- see this module's docstring. A real
    # multi-worker fleet would need to query every known worker and merge;
    # out of scope for this pass (documented gap, not silently accepted).
    resp = await _proxy(
        wiring,
        "GET",
        wiring.worker_base_url,
        f"/internal/sessions?tenant={authed.tenant}",
    )
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )


@router.post("/{session_id}/execute")
async def execute_session(
    session_id: str,
    request: Request,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    requests_total.labels(tenant=authed.tenant, route="execute_session").inc()
    worker_url = await resolve_worker(wiring, session_id)
    body = await request.body()
    resp = await _proxy(
        wiring,
        "POST",
        worker_url,
        f"/internal/sessions/{session_id}/execute",
        content=body,
        headers={"content-type": "application/json"},
    )
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )


@router.delete("/{session_id}")
async def release_session(
    session_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    requests_total.labels(tenant=authed.tenant, route="release_session").inc()
    worker_url = await resolve_worker(wiring, session_id)
    resp = await _proxy(wiring, "DELETE", worker_url, f"/internal/sessions/{session_id}")
    assert wiring.redis is not None
    await wiring.redis.delete(session_route_key(session_id))
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )
