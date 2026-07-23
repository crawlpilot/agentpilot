"""`gateway`-role's `/v1/sessions/...` -- a thin reverse proxy to whichever
worker a session actually lives on (the exact same route implementations in
`routes/sessions.py`, just mounted under a different prefix on the worker).

Mounted **instead of** `routes/sessions.py` when `wiring.role == "gateway"`
(see `app.py`) -- a gateway process never imports/touches `agentpilot.driver`.
Every route here requires `require_tenant_auth` (added at `include_router()`
time in `app.py`, not per-function, since this router is *only* ever mounted
at `/v1/sessions` -- there's no internal/trusted-mount ambiguity to handle the
way `routes/sessions.py` does): the gateway is the tenant-facing edge, so it's
the one place a real `tenant` has to come from an authenticated credential,
never a free-text request field. The authed tenant overwrites `req.tenant`
before forwarding, so the worker's internal surface can go on trusting its
caller completely -- the gateway is the only thing allowed to call it.

**Placement**: real, multi-worker, via `wiring.placer` (`agentpilot.placement.
placer.SessionPlacer`) -- affinity-first, then least-loaded, with admission
control, atomically reserved in Redis before the (possibly multi-second)
worker HTTP call. `open_session` is necessarily two-phase (reserve a node,
*then* proxy, *then* commit the route) because the worker -- not the gateway
-- mints `session_id`; see `placer.py`'s module docstring for why that rules
out a single atomic "place and route" script.

Live-view WebSocket proxying lives in `routes/live_view_proxy.py`; CDP
proxying in `routes/cdp_proxy.py` -- both resolve routes via
`agentpilot.gateway.routing.resolve_route` the same way this module does.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.routing import resolve_node_addr, resolve_route, session_route_key
from agentpilot.gateway.schemas import SessionOpenRequest
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.observability.metrics import requests_total, session_open_duration_seconds
from agentpilot.spi.errors import NodeLost
from agentpilot.spi.identity import IdentityKey

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions-gateway-proxy"])


async def _proxy(
    wiring: Wiring, method: str, addr: str, path: str, **kwargs: object
) -> httpx.Response:
    url = f"{addr}{path}"
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

    identity = IdentityKey(tenant=req.tenant, domain=req.domain, name=req.name)
    node_id = await wiring.placer.place(identity, wiring.affinity_ttl_seconds)
    try:
        addr = await resolve_node_addr(wiring, node_id)
    except NodeLost:
        # Half-dead: died between a fresh place() and this lookup. One
        # retry against a fresh candidate, bounded to one so a sick node
        # can't absorb fleet latency (plan.md's "Node failure" section).
        await wiring.placer.release_reservation(node_id)
        node_id = await wiring.placer.place(identity, wiring.affinity_ttl_seconds)
        addr = await resolve_node_addr(wiring, node_id)

    with session_open_duration_seconds.time():
        resp = await _proxy(wiring, "POST", addr, "/internal/sessions", json=req.model_dump())

    if resp.status_code == 200:
        session_id = resp.json()["session_id"]
        await wiring.placer.commit_route(
            session_id, node_id, identity, req.tier, wiring.lease_ttl_seconds
        )
    else:
        await wiring.placer.release_reservation(node_id)

    log.info("gateway_proxy.open", duration_ms=(time.monotonic() - started) * 1000, node_id=node_id)
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )


@router.get("")
async def list_sessions(
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    assert wiring.redis is not None
    node_ids = [
        n.decode() if isinstance(n, bytes) else n
        for n in await wiring.redis.smembers("live_nodes")
    ]

    async def _fetch(node_id: str) -> list[dict]:
        try:
            addr = await resolve_node_addr(wiring, node_id)
            resp = await wiring.http_client.get(
                f"{addr}/internal/sessions", params={"tenant": authed.tenant}
            )
            resp.raise_for_status()
            return list(resp.json().get("sessions", []))
        except Exception as exc:
            log.warning("gateway_proxy.list_sessions_node_unreachable", node_id=node_id, error=str(exc))
            return []

    results = await asyncio.gather(*(_fetch(n) for n in node_ids))
    sessions = [s for batch in results for s in batch]
    return Response(
        content=json.dumps({"sessions": sessions}).encode(),
        status_code=200,
        media_type="application/json",
    )


@router.post("/{session_id}/execute")
async def execute_session(
    session_id: str,
    request: Request,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    requests_total.labels(tenant=authed.tenant, route="execute_session").inc()
    _node_id, addr = await resolve_route(wiring, session_id, authed.tenant)
    body = await request.body()
    resp = await _proxy(
        wiring,
        "POST",
        addr,
        f"/internal/sessions/{session_id}/execute",
        content=body,
        headers={"content-type": "application/json"},
    )
    if resp.status_code == 200:
        # Route TTL renewal -- the worker's own registry.renew() call (fired
        # by this same request) only renews the *lease*; it never touches
        # the gateway's separate session:{id} route hash.
        assert wiring.redis is not None
        await wiring.redis.expire(session_route_key(session_id), wiring.lease_ttl_seconds)
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
    node_id, addr = await resolve_route(wiring, session_id, authed.tenant)
    resp = await _proxy(wiring, "DELETE", addr, f"/internal/sessions/{session_id}")
    assert wiring.redis is not None
    await wiring.redis.delete(session_route_key(session_id))
    await wiring.redis.srem(f"node_sessions:{node_id}", session_id)
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )
