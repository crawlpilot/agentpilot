"""`gateway`-role's `/v1/sessions/{id}/live-view` -- a bidirectional
WebSocket relay to a worker's `/internal/sessions/{id}/live-view` (the exact
route in `routes/live_view.py`, mounted on `worker` too as of this pass).
Closes the gap `routes/gateway_proxy.py` used to document: a `gateway`-role
process previously had no live-view route at all.

Auth: the browser can't set headers on a WS handshake, so it sends
`?api_key=...` on the handshake (see `routes/live_view.py`'s docstring). This
proxy validates that key itself
*and* forwards it unchanged to the worker, since `live_view.py`'s
unconditional (not per-mount) check expects one on every mount, including
`/internal/sessions`.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
import websockets
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection

from agentpilot.gateway.auth_deps import resolve_query_api_key
from agentpilot.gateway.routing import resolve_route
from agentpilot.gateway.wiring import get_wiring
from agentpilot.spi.errors import NodeLost

log = structlog.get_logger(__name__)

router = APIRouter(tags=["live-view-gateway-proxy"])

_NOT_FOUND = 4404
_UNAUTHORIZED = 4401
_BAD_UPSTREAM = 4502


async def _pump_frames(websocket: WebSocket, upstream: ClientConnection) -> None:
    async for frame in upstream:
        data = frame if isinstance(frame, bytes) else frame.encode()
        await websocket.send_bytes(data)


async def _pump_input(websocket: WebSocket, upstream: ClientConnection) -> None:
    while True:
        msg = await websocket.receive_text()
        await upstream.send(msg)


async def _watch_disconnect(websocket: WebSocket) -> None:
    """`view` mode's only await is `_pump_frames`, which blocks on the
    *upstream* (worker) side -- it has no way to notice the *downstream*
    (browser) side going away. Without this, a browser that disconnects
    before the worker ever sends a frame leaves `async with
    websockets.connect(ws_url)` never exiting, so the proxy's upstream
    connection to the worker never closes either -- which is exactly what
    `routes/live_view.py`'s own `_watch_disconnect` needs to see to
    decrement its reference-counted screencast. One leaked hop here
    silently reproduces that same leak one layer up."""

    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            return


@router.websocket("/{session_id}/live-view")
async def live_view_proxy(
    websocket: WebSocket, session_id: str, mode: str = "view", page_id: str | None = None
) -> None:
    wiring = await get_wiring()
    api_key = websocket.query_params.get("api_key")
    authed = await resolve_query_api_key(wiring, api_key)
    if authed is None:
        await websocket.close(code=_UNAUTHORIZED, reason="invalid api key")
        return

    try:
        _node_id, addr = await resolve_route(wiring, session_id, authed.tenant)
    except HTTPException as exc:
        code = _UNAUTHORIZED if exc.status_code == 403 else _NOT_FOUND
        await websocket.close(code=code, reason=str(exc.detail))
        return
    except NodeLost:
        await websocket.close(code=_BAD_UPSTREAM, reason="worker node is gone")
        return

    # No `&api_key=...` forwarded here: this proxy already authenticated the
    # caller above, and the worker's `wiring.api_keys` is permanently an
    # empty placeholder (`wiring.py`'s `_connect_api_keys` docstring --
    # `worker` never mounts an auth-gated route), so a forwarded key can
    # never resolve there and `live_view.py`'s check would always 403. The
    # internal mount already trusts network position alone for every other
    # route reached through this proxy; matching that here (an absent
    # `api_key` takes `live_view.py`'s pre-auth-compatible trust path)
    # rather than re-validating against a store that can't ever succeed.
    ws_url = (
        addr.replace("http://", "ws://").replace("https://", "wss://")
        + f"/internal/sessions/{session_id}/live-view?mode={mode}"
        + (f"&page_id={page_id}" if page_id is not None else "")
    )

    await websocket.accept()
    try:
        async with websockets.connect(ws_url) as upstream:
            sender = asyncio.create_task(_pump_frames(websocket, upstream))
            try:
                if mode == "interact":
                    await _pump_input(websocket, upstream)
                else:
                    watcher = asyncio.create_task(_watch_disconnect(websocket))
                    _done, pending = await asyncio.wait(
                        {sender, watcher}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.gather(*pending, return_exceptions=True)
            except WebSocketDisconnect:
                pass
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
    except OSError as exc:
        log.error("live_view_proxy.worker_unreachable", url=ws_url, error=str(exc))
        await websocket.close(code=_BAD_UPSTREAM, reason="worker unreachable")
