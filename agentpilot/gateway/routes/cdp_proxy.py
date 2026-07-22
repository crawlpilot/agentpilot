"""`gateway`-role's `/v1/sessions/{id}/cdp[...]` -- proxies to a worker's
`/internal/sessions/{id}/cdp[...]` counterpart in `routes/cdp.py`.

Unlike `gateway_proxy.py`'s other routes, the discovery GET must NOT do a
pure byte passthrough on success: the worker's own rewritten
`webSocketDebuggerUrl` points at whatever the worker thinks its own public
address is, which is never externally reachable -- the gateway is the true
public edge and must have the final say on that field. On a non-200 from the
worker, the body is still passed through unchanged (matching
`gateway_proxy.py`'s existing error-passthrough behavior) rather than trying
to parse an error envelope as a success payload.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import structlog
import websockets
from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.auth_deps import bearer_token, require_tenant_auth, resolve_query_api_key
from agentpilot.gateway.routing import resolve_worker
from agentpilot.gateway.wiring import Wiring, get_wiring

log = structlog.get_logger(__name__)

router = APIRouter(tags=["cdp-gateway-proxy"])

_NOT_FOUND = 4404
_UNAUTHORIZED = 4401
_BAD_UPSTREAM = 4502


@router.get("/{session_id}/cdp/json/version")
async def cdp_json_version_proxy(
    session_id: str,
    request: Request,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    worker_url = await resolve_worker(wiring, session_id)
    resp = await wiring.http_client.get(
        f"{worker_url}/internal/sessions/{session_id}/cdp/json/version"
    )
    if resp.status_code != 200:
        return Response(
            content=resp.content, status_code=resp.status_code, media_type="application/json"
        )

    payload = resp.json()
    token = bearer_token(request.headers.get("authorization"))
    ws_base = (
        str(request.base_url).replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    )
    url = f"{ws_base}/v1/sessions/{session_id}/cdp"
    payload["webSocketDebuggerUrl"] = f"{url}?api_key={token}" if token is not None else url
    return Response(
        content=json.dumps(payload).encode(), status_code=200, media_type="application/json"
    )


async def _pump_downstream_to_upstream(websocket: WebSocket, upstream: ClientConnection) -> None:
    while True:
        msg = await websocket.receive_text()
        await upstream.send(msg)


async def _pump_upstream_to_downstream(websocket: WebSocket, upstream: ClientConnection) -> None:
    async for msg in upstream:
        await websocket.send_text(msg if isinstance(msg, str) else msg.decode())


@router.websocket("/{session_id}/cdp")
async def cdp_relay_proxy(websocket: WebSocket, session_id: str) -> None:
    wiring = await get_wiring()
    api_key = websocket.query_params.get("api_key")
    authed = await resolve_query_api_key(wiring, api_key)
    if authed is None:
        await websocket.close(code=_UNAUTHORIZED, reason="invalid api key")
        return

    try:
        worker_url = await resolve_worker(wiring, session_id)
    except Exception:
        await websocket.close(code=_NOT_FOUND, reason="no such session")
        return

    # No `&api_key=...` forwarded here: same reasoning as
    # `live_view_proxy.py` -- the internal mount trusts network position, and
    # the worker's `wiring.api_keys` is a permanent empty placeholder there.
    ws_url = (
        worker_url.replace("http://", "ws://").replace("https://", "wss://")
        + f"/internal/sessions/{session_id}/cdp"
    )

    await websocket.accept()
    try:
        async with websockets.connect(ws_url, max_size=None) as upstream:
            sender = asyncio.create_task(_pump_downstream_to_upstream(websocket, upstream))
            try:
                await _pump_upstream_to_downstream(websocket, upstream)
            except WebSocketDisconnect:
                pass
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
    except OSError as exc:
        log.error("cdp_proxy.worker_unreachable", url=ws_url, error=str(exc))
        await websocket.close(code=_BAD_UPSTREAM, reason="worker unreachable")
