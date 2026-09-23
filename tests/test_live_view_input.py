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

import pytest

from agentpilot.gateway.routes.live_view import _parse_input_event
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
