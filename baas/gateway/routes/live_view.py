"""`GET /{id}/live-view` (WebSocket) -- CDP screencast. `mode=view` streams
frames only; `mode=interact` also accepts inbound `InputEvent` JSON messages
and dispatches them via the driver.

No baked-in path prefix, same reasoning as `routes/sessions.py`: `app.py`
mounts this router at both `/v1/sessions` (monolith, tenant-facing) and
`/internal/sessions` (worker/monolith-internal). Browsers can't set custom
headers on a WS handshake, so the tenant credential travels as `?api_key=...`
instead of `Authorization: Bearer` -- checked here, unconditionally, on
every mount, not gated per-mount like the HTTP routes. This means the
internal mount is slightly more locked-down than `sessions.py`'s internal
surface (which trusts network position alone): a `gateway`-role process's
`live_view_proxy.py` forwards the *same* `api_key` it validated from the
browser through to the worker, rather than the worker trusting the proxy
implicitly, since this is the one route a browser reaches (almost) directly
through the proxy. If `api_key` is absent entirely, the call is trusted as
before (covers the `test_seam_e2e.py`/pre-auth compatibility path and any
same-process caller that has no key at all).

`LiveViewCapable` is an *optional* capability: a driver without it simply
gets a clean close here rather than a route that was never wired up, per
the plan's composition-root check.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from baas.gateway.auth_deps import resolve_query_api_key
from baas.gateway.wiring import get_wiring
from baas.spi.streaming import (
    InputEvent,
    KeyEvent,
    LiveViewCapable,
    MouseButtonEvent,
    MouseMoveEvent,
    WheelEvent,
)

router = APIRouter(tags=["live-view"])
_UNAUTHORIZED = 4401

_NOT_FOUND = 4404
_UNSUPPORTED = 4501


def _parse_input_event(msg: dict[str, Any]) -> InputEvent | None:
    kind = msg.get("kind")
    if kind == "mousemove":
        return MouseMoveEvent(x=msg["x"], y=msg["y"])
    if kind in ("mousedown", "mouseup"):
        return MouseButtonEvent(
            x=msg["x"],
            y=msg["y"],
            button=msg.get("button", "left"),
            action="down" if kind == "mousedown" else "up",
        )
    if kind == "wheel":
        return WheelEvent(x=msg["x"], y=msg["y"], delta_x=msg["deltaX"], delta_y=msg["deltaY"])
    if kind in ("keydown", "keyup"):
        return KeyEvent(key=msg["key"], action="down" if kind == "keydown" else "up")
    return None


async def _send_frames(websocket: WebSocket, queue: asyncio.Queue[Any]) -> None:
    """Stops quietly once the socket is closing rather than letting a send
    into a closing connection raise (observed as `websockets.exceptions.
    InvalidState` spam in logs when a client disconnects mid-stream)."""

    while True:
        frame = await queue.get()
        try:
            await websocket.send_bytes(frame.data)
        except Exception:
            return


async def _receive_input(websocket: WebSocket, driver: LiveViewCapable, ctx: Any) -> None:
    while True:
        msg = await websocket.receive_json()
        event = _parse_input_event(msg)
        if event is not None:
            await driver.dispatch_input(ctx, event)


@router.websocket("/{session_id}/live-view")
async def live_view(
    websocket: WebSocket, session_id: str, mode: str = "view", api_key: str | None = None
) -> None:
    wiring = await get_wiring()
    session = wiring.sessions.get(session_id)
    if session is None:
        await websocket.close(code=_NOT_FOUND, reason="no such session")
        return

    if api_key is not None:
        authed = await resolve_query_api_key(wiring, api_key)
        if authed is None or authed.tenant != session.identity.tenant:
            await websocket.close(code=_UNAUTHORIZED, reason="invalid api key")
            return

    driver = wiring.driver
    if not isinstance(driver, LiveViewCapable):
        await websocket.close(code=_UNSUPPORTED, reason="driver does not support live view")
        return

    await websocket.accept()
    queue = await driver.start_screencast(session.ctx)
    sender = asyncio.create_task(_send_frames(websocket, queue))
    try:
        if mode == "interact":
            await _receive_input(websocket, driver, session.ctx)
        else:
            await sender
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        await driver.stop_screencast(session.ctx)
