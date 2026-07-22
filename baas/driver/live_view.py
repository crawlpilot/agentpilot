"""CDP screencast + human input dispatch -- Page and Input domains only,
**never** Runtime, to preserve Patchright's anti-leak guarantee. Standing
constraint: nothing in this module (or its callers) may send a `Runtime.*`
CDP command on a live-view session's `CDPSession`.

Pure, driver-agnostic conversions live here (unit-testable with no browser);
`PatchrightDriver` owns the actual CDP session lifecycle since it already
tracks per-context state.
"""

from __future__ import annotations

import base64
import time
from typing import Any

from baas.spi.streaming import (
    InputEvent,
    KeyEvent,
    LiveViewFrame,
    MouseButtonEvent,
    MouseMoveEvent,
    WheelEvent,
)

SCREENCAST_START_PARAMS = {"format": "jpeg", "quality": 80, "everyNthFrame": 1}


def parse_screencast_frame(params: dict[str, Any]) -> LiveViewFrame:
    metadata = params["metadata"]
    return LiveViewFrame(
        data=base64.b64decode(params["data"]),
        format="jpeg",
        width=metadata["deviceWidth"],
        height=metadata["deviceHeight"],
        ts=metadata.get("timestamp", time.time()),
    )


def to_cdp_input_params(event: InputEvent) -> tuple[str, dict[str, Any]]:
    """Returns (cdp_method, params) for `cdp_session.send(...)`."""

    if isinstance(event, MouseMoveEvent):
        return "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": event.x, "y": event.y}
    if isinstance(event, MouseButtonEvent):
        cdp_type = "mousePressed" if event.action == "down" else "mouseReleased"
        return "Input.dispatchMouseEvent", {
            "type": cdp_type,
            "x": event.x,
            "y": event.y,
            "button": event.button,
            "clickCount": 1,
        }
    if isinstance(event, WheelEvent):
        return "Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": event.x,
            "y": event.y,
            "deltaX": event.delta_x,
            "deltaY": event.delta_y,
        }
    if isinstance(event, KeyEvent):
        cdp_type = "keyDown" if event.action == "down" else "keyUp"
        return "Input.dispatchKeyEvent", {"type": cdp_type, "key": event.key}
    raise NotImplementedError(f"unhandled input event type: {type(event).__name__}")
