"""`GET /v1/sessions/{id}/live-view` (WebSocket) -- gateway-proxied CDP
screencast. `mode=view` streams frames only; `mode=interact` also accepts
inbound `InputEvent` JSON messages and dispatches them via the driver.

Same tenant-facing auth surface as `routes/sessions.py` (P0 has no real
tenant auth yet -- this only checks the session exists, matching that route).
`LiveViewCapable` is an *optional* capability: a driver without it simply
gets a clean close here rather than a route that was never wired up, per
the plan's composition-root check.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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


async def _send_frames(websocket: WebSocket, queue: "asyncio.Queue[Any]") -> None:
    while True:
        frame = await queue.get()
        await websocket.send_bytes(frame.data)


async def _receive_input(websocket: WebSocket, driver: LiveViewCapable, ctx: Any) -> None:
    while True:
        msg = await websocket.receive_json()
        event = _parse_input_event(msg)
        if event is not None:
            await driver.dispatch_input(ctx, event)


@router.websocket("/v1/sessions/{session_id}/live-view")
async def live_view(websocket: WebSocket, session_id: str, mode: str = "view") -> None:
    wiring = get_wiring()
    session = wiring.sessions.get(session_id)
    if session is None:
        await websocket.close(code=_NOT_FOUND, reason="no such session")
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
