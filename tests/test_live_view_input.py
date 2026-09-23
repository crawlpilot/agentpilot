"""Input events arriving from a browser, on their way to CDP.

This is a trust boundary: the message is JSON from a web page and everything
past it is a `Input.dispatchMouseEvent` call. It was taking `msg["x"]` and
`msg.get("button", "left")` straight through.

What that cost, measured: `LiveViewCanvas` computes image-space coordinates by
dividing by `img.naturalWidth`, which is 0 until the first screencast frame has
decoded. The result is `NaN` -- and `JSON.stringify` does not fail on `NaN`, it
writes `null`. So the socket sent `{"x": null, "y": null}`, Chrome answered
`Invalid parameters`, and the error propagated out of the route handler and
closed the connection. Clicking in the live view simply stopped working.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentpilot.gateway.errors import register_exception_handlers
from agentpilot.gateway.routes import live_view as live_view_module
from agentpilot.gateway.routes.live_view import _parse_input_event
from crawlpilot.spi.errors import TabNotFound
from crawlpilot.spi.streaming import (
    KeyEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    WheelEvent,
)


def test_an_ordinary_click_parses() -> None:
    event = _parse_input_event(
        {"kind": "mousedown", "x": 412.5, "y": 208.0, "button": "left"}
    )
    assert isinstance(event, MouseButtonEvent)
    assert (event.x, event.y, event.button, event.action) == (412.5, 208.0, "left", "down")


@pytest.mark.parametrize("kind", ["mousemove", "mousedown", "mouseup", "wheel"])
def test_a_null_coordinate_is_dropped_rather_than_sent_to_chrome(kind: str) -> None:
    """THE regression. `JSON.stringify({x: NaN})` emits `{"x": null}`, so this is
    exactly what a canvas that has not yet decoded a frame puts on the wire."""

    msg = {"kind": kind, "x": None, "y": None, "deltaX": 0, "deltaY": 0}
    assert _parse_input_event(msg) is None


@pytest.mark.parametrize("bad", [None, "120", float("nan"), float("inf"), [], {}, True])
def test_a_coordinate_that_is_not_a_finite_number_is_dropped(bad: object) -> None:
    """`True` is in here on purpose: `isinstance(True, int)` is True in Python,
    so a bool would otherwise sail through as the coordinate 1."""

    assert _parse_input_event({"kind": "mousedown", "x": bad, "y": 10}) is None
    assert _parse_input_event({"kind": "mousedown", "x": 10, "y": bad}) is None


def test_a_numeric_button_is_dropped() -> None:
    """The DOM's `MouseEvent.button` is 0/1/2 and CDP's is an enum string. The
    `Literal` on `MouseButtonEvent` is a dataclass annotation and enforces
    nothing, so a client sending the DOM value would put `0` into a CDP call."""

    assert _parse_input_event({"kind": "mousedown", "x": 1, "y": 2, "button": 0}) is None
    assert _parse_input_event({"kind": "mousedown", "x": 1, "y": 2, "button": "LEFT"}) is None


def test_wheel_deltas_are_checked_too() -> None:
    assert _parse_input_event(
        {"kind": "wheel", "x": 1, "y": 2, "deltaX": None, "deltaY": 40}
    ) is None
    event = _parse_input_event({"kind": "wheel", "x": 1, "y": 2, "deltaX": 0, "deltaY": 40})
    assert isinstance(event, WheelEvent)
    assert event.delta_y == 40


def test_an_integer_coordinate_is_accepted_as_a_float() -> None:
    event = _parse_input_event({"kind": "mousemove", "x": 10, "y": 20})
    assert isinstance(event, MouseMoveEvent)
    assert (event.x, event.y) == (10.0, 20.0)


def test_keys_still_parse_and_an_empty_one_does_not() -> None:
    event = _parse_input_event({"kind": "keydown", "key": "Enter"})
    assert isinstance(event, KeyEvent)
    assert (event.key, event.action) == ("Enter", "down")
    assert _parse_input_event({"kind": "keydown", "key": ""}) is None
    assert _parse_input_event({"kind": "keydown"}) is None


def test_an_unknown_kind_is_ignored() -> None:
    assert _parse_input_event({"kind": "teleport", "x": 1, "y": 2}) is None
    assert _parse_input_event({}) is None


# --- the route's own post-`accept()` failure paths (see
# `tests/test_websocket_error_handling.py` for the ASGI-level regression) ---


class _FakeDriver:
    """Satisfies `LiveViewCapable` (a runtime-checkable Protocol) with
    whatever failure the test under it needs."""

    def __init__(self, *, start_error: Exception | None = None,
                 stop_error: Exception | None = None) -> None:
        self._start_error = start_error
        self._stop_error = stop_error
        self.stopped = False

    async def start_screencast(self, ctx: object, page_id: str | None = None) -> asyncio.Queue:
        if self._start_error is not None:
            raise self._start_error
        return asyncio.Queue()

    async def stop_screencast(self, ctx: object, page_id: str | None = None) -> None:
        self.stopped = True
        if self._stop_error is not None:
            raise self._stop_error

    async def dispatch_input(self, ctx: object, event: object, page_id: str | None = None) -> None:
        return None


def _route_app(driver: _FakeDriver) -> tuple[FastAPI, SimpleNamespace]:
    session = SimpleNamespace(ctx=object(), identity=None)
    wiring = SimpleNamespace(sessions={"s1": session}, driver=driver)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(live_view_module.router, prefix="/internal/sessions")
    return app, wiring


def test_a_tab_that_vanished_closes_4404_instead_of_crashing_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression. `useLiveView` reconnects as soon as it resolves a
    `page_id`, so a tab closed in between is a routine race -- and the error
    lands *after* `accept()`, where an app-level handler's `JSONResponse` is
    an ASGI protocol violation rather than an answer."""

    driver = _FakeDriver(start_error=TabNotFound("no such tab 'abc'"))
    app, wiring = _route_app(driver)

    async def _fake_wiring() -> object:
        return wiring

    monkeypatch.setattr(live_view_module, "get_wiring", _fake_wiring)

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: PT012
        with client.websocket_connect("/internal/sessions/s1/live-view?page_id=abc") as ws:
            ws.receive_bytes()
    assert excinfo.value.code == 4404
    assert not driver.stopped  # nothing was started, so nothing to tear down


def test_a_tab_that_vanishes_mid_stream_does_not_fail_the_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stop_screencast` re-resolves the page too, so the same race hits the
    `finally` -- where a raise would replace the connection's real ending."""

    driver = _FakeDriver(stop_error=TabNotFound("no such tab 'abc'"))
    app, wiring = _route_app(driver)

    async def _fake_wiring() -> object:
        return wiring

    monkeypatch.setattr(live_view_module, "get_wiring", _fake_wiring)

    client = TestClient(app)
    with client.websocket_connect("/internal/sessions/s1/live-view?page_id=abc"):
        pass
    assert driver.stopped
