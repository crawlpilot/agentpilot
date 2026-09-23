"""`GET /{id}/live-view` (WebSocket) -- CDP screencast. `mode=view` streams
frames only; `mode=interact` also accepts inbound `InputEvent` JSON messages
and dispatches them via the driver.

No baked-in path prefix, same reasoning as `routes/sessions.py`: `app.py`
mounts this router only on the `worker` role, at `/internal/sessions`; a
`gateway` serves the tenant-facing `/v1/sessions/.../live-view` via
`routes/live_view_proxy.py`. Browsers can't set custom
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
import math
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from agentpilot.control.identity import tenant_of
from agentpilot.gateway.auth_deps import resolve_query_api_key
from agentpilot.gateway.wiring import get_wiring
from crawlpilot.spi.errors import DriverError
from crawlpilot.spi.streaming import (
    InputEvent,
    KeyEvent,
    LiveViewCapable,
    MouseButtonEvent,
    MouseMoveEvent,
    WheelEvent,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["live-view"])
_UNAUTHORIZED = 4401

_NOT_FOUND = 4404
_UNSUPPORTED = 4501


# Buttons `Input.dispatchMouseEvent` accepts. The `Literal` on
# `MouseButtonEvent` is a dataclass annotation and enforces nothing at runtime,
# and what arrives here is a JSON message from a browser -- so a client sending
# the DOM's numeric `MouseEvent.button` would put `0` straight into a CDP call.
_BUTTONS = frozenset({"left", "right", "middle"})


def _coord(value: Any) -> float | None:
    """One screen coordinate, or None if it is not a usable number.

    This is the check that was missing, and it cost a live view that silently
    would not accept clicks. `JSON.stringify` does not fail on `NaN` or
    `Infinity` -- it writes `null` -- so a browser whose canvas had not yet
    decoded its first frame sent `{"x": null, "y": null}`, which reached Chrome
    as `Input.dispatchMouseEvent` with a null coordinate and came back
    `Invalid parameters`. That error propagated out of the route handler and
    closed the websocket, so the live view stopped responding entirely.

    The client is fixed not to produce those, but this is the trust boundary:
    the message comes from a browser, and everything past here goes to CDP.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_input_event(msg: dict[str, Any]) -> InputEvent | None:
    kind = msg.get("kind")

    if kind in ("mousemove", "mousedown", "mouseup", "wheel"):
        x, y = _coord(msg.get("x")), _coord(msg.get("y"))
        if x is None or y is None:
            log.debug("live_view.bad_coordinates", kind=kind, x=msg.get("x"), y=msg.get("y"))
            return None
        if kind == "mousemove":
            return MouseMoveEvent(x=x, y=y)
        if kind == "wheel":
            dx, dy = _coord(msg.get("deltaX")), _coord(msg.get("deltaY"))
            if dx is None or dy is None:
                return None
            return WheelEvent(x=x, y=y, delta_x=dx, delta_y=dy)
        button = msg.get("button", "left")
        if button not in _BUTTONS:
            log.debug("live_view.bad_button", button=button)
            return None
        return MouseButtonEvent(
            x=x, y=y, button=button,
            action="down" if kind == "mousedown" else "up",
        )

    if kind in ("keydown", "keyup"):
        key = msg.get("key")
        if not isinstance(key, str) or not key:
            return None
        return KeyEvent(key=key, action="down" if kind == "keydown" else "up")
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


async def _receive_input(
    websocket: WebSocket, driver: LiveViewCapable, ctx: Any, page_id: str | None
) -> None:
    while True:
        msg = await websocket.receive_json()
        event = _parse_input_event(msg)
        if event is None:
            continue
        try:
            await driver.dispatch_input(ctx, event, page_id)
        except Exception as exc:  # noqa: BLE001 - see below
            # One rejected event must not take the session down with it. This
            # used to propagate through the route handler and close the socket,
            # so a single malformed click ended the live view -- and the person
            # watching saw a page that had simply stopped responding, with the
            # reason only in a worker's log.
            log.warning(
                "live_view.input_rejected",
                kind=type(event).__name__, error=str(exc),
            )


async def _watch_disconnect(websocket: WebSocket) -> None:
    """`view` mode never expects inbound messages, but a client-initiated
    close is *only* ever surfaced through `receive()` -- without this,
    `await sender` alone can block forever once `_send_frames`' `queue.get()`
    has nothing left to feed it (the client is long gone, or the screencast
    never produced a frame to begin with). That leaves this connection's
    `finally: stop_screencast()` never running, which -- since
    `start_screencast`/`stop_screencast` are reference-counted precisely so
    a reconnect can share an in-flight screencast -- permanently leaks this
    connection's reference and wedges every future connection to the same
    page behind a queue nothing will ever feed again."""

    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            return


@router.websocket("/{session_id}/live-view")
async def live_view(
    websocket: WebSocket,
    session_id: str,
    mode: str = "view",
    api_key: str | None = None,
    page_id: str | None = None,
) -> None:
    wiring = await get_wiring()
    session = wiring.sessions.get(session_id)
    if session is None:
        await websocket.close(code=_NOT_FOUND, reason="no such session")
        return

    if api_key is not None:
        authed = await resolve_query_api_key(wiring, api_key)
        if authed is None or authed.tenant != tenant_of(session.identity):
            await websocket.close(code=_UNAUTHORIZED, reason="invalid api key")
            return

    driver = wiring.driver
    if not isinstance(driver, LiveViewCapable):
        await websocket.close(code=_UNSUPPORTED, reason="driver does not support live view")
        return

    await websocket.accept()
    try:
        queue = await driver.start_screencast(session.ctx, page_id)
    except DriverError as exc:
        # The page can be gone between the client resolving a `page_id` and
        # this connection reaching the driver -- a tab the script closed, or a
        # session torn down mid-reconnect, which `useLiveView`'s
        # reconnect-on-`page_id`-resolution dance makes a routine race rather
        # than a rare one. Uncaught, this escaped into the app-level handlers
        # in `gateway/errors.py`, which answer every `DriverError` with a
        # `JSONResponse`: on an already-accepted websocket that is an ASGI
        # protocol violation (`Expected ASGI message 'websocket.send' ...`),
        # so uvicorn killed the connection with a bare 1006 -- which
        # `liveView.ts` reads as "retry", reconnecting into the same
        # `TabNotFound` every second forever. 4404 is one of its terminal
        # codes, so the client stops and shows the error.
        log.info(
            "live_view.page_gone",
            session_id=session_id,
            page_id=page_id,
            error=str(exc),
        )
        await websocket.close(code=_NOT_FOUND, reason="no such page")
        return
    sender = asyncio.create_task(_send_frames(websocket, queue))
    try:
        if mode == "interact":
            await _receive_input(websocket, driver, session.ctx, page_id)
        else:
            watcher = asyncio.create_task(_watch_disconnect(websocket))
            _done, pending = await asyncio.wait({sender, watcher}, return_when=asyncio.FIRST_COMPLETED)
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
        # Same race as `start_screencast` above, one step later: the tab can
        # close while the stream is running, and teardown's own
        # `_require_page` raises then too. A failed teardown must not become
        # the connection's exit path.
        with contextlib.suppress(DriverError):
            await driver.stop_screencast(session.ctx, page_id)
