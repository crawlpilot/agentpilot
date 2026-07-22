"""`gateway`-role's `/v1/sessions/...` -- a thin reverse proxy to a worker's
`/internal/sessions/...` (the exact same route implementations in
`routes/sessions.py`, just mounted under a different prefix on the worker).

Mounted **instead of** `routes/sessions.py` when `wiring.role == "gateway"`
(see `app.py`) -- a gateway process never imports/touches `baas.driver`.

**Placement**: trivially "the one configured worker"
(`wiring.worker_base_url`) -- `plan.md`'s real placement (affinity,
capacity-weighted, node failure) needs more than one worker to place
across, which this pass doesn't have running. The routing-table mechanism
(`session:{id} -> worker_addr` in Redis) is still real and would extend to
multiple workers without a shape change; only the *decision* is a stub.

**Not built in this pass**: live-view WebSocket proxying (bidirectional
frame/input relay gateway<->worker) -- a `gateway`-role process has no
`/v1/sessions/{id}/live-view` route at all yet. Noted as a real gap, not
silently dropped; `monolith`/`worker` roles still serve it directly.
"""

from __future__ import annotations

import time

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from baas.gateway.schemas import SessionOpenRequest
from baas.gateway.wiring import Wiring, get_wiring
from baas.observability.metrics import requests_total, session_open_duration_seconds

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions-gateway-proxy"])


def _session_route_key(session_id: str) -> str:
    return f"session:{session_id}"


async def _proxy(
    wiring: Wiring, method: str, worker_url: str, path: str, **kwargs: object
) -> httpx.Response:
    url = f"{worker_url}{path}"
    try:
        return await wiring.http_client.request(method, url, **kwargs)  # type: ignore[arg-type]
    except httpx.TransportError as exc:
        log.error("gateway_proxy.worker_unreachable", url=url, error=str(exc))
        raise HTTPException(status_code=502, detail="worker unreachable") from exc


async def _resolve_worker(wiring: Wiring, session_id: str) -> str:
    assert wiring.redis is not None  # role=="gateway" always has BAAS_REDIS_URL
    raw = await wiring.redis.get(_session_route_key(session_id))
    if raw is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
    return raw.decode() if isinstance(raw, bytes) else raw


@router.post("")
async def open_session(req: SessionOpenRequest, wiring: Wiring = Depends(get_wiring)) -> Response:
    requests_total.labels(tenant=req.tenant, route="open_session").inc()
    started = time.monotonic()
    # Trivial single-worker "placement" -- see this module's docstring.
    worker_url = wiring.worker_base_url
    with session_open_duration_seconds.time():
        resp = await _proxy(wiring, "POST", worker_url, "/internal/sessions", json=req.model_dump())
    if resp.status_code == 200:
        session_id = resp.json()["session_id"]
        assert wiring.redis is not None
        await wiring.redis.set(_session_route_key(session_id), worker_url)
    log.info("gateway_proxy.open", duration_ms=(time.monotonic() - started) * 1000)
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )


@router.post("/{session_id}/execute")
async def execute_session(
    session_id: str, request: Request, wiring: Wiring = Depends(get_wiring)
) -> Response:
    requests_total.labels(tenant="unknown", route="execute_session").inc()
    worker_url = await _resolve_worker(wiring, session_id)
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
async def release_session(session_id: str, wiring: Wiring = Depends(get_wiring)) -> Response:
    requests_total.labels(tenant="unknown", route="release_session").inc()
    worker_url = await _resolve_worker(wiring, session_id)
    resp = await _proxy(wiring, "DELETE", worker_url, f"/internal/sessions/{session_id}")
    assert wiring.redis is not None
    await wiring.redis.delete(_session_route_key(session_id))
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )
