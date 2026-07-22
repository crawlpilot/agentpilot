"""Live-view types (see plan.md's Live View section): CDP screencast frames
outbound, human input events inbound. Deliberately **not** reusing
`actions.Action` -- agents target refs, a human targets pixels.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from agentpilot.spi.lease import ContextRef


@dataclass
class LiveViewFrame:
    data: bytes
    format: Literal["jpeg", "png"]
    width: int
    height: int
    ts: float


@dataclass
class MouseMoveEvent:
    x: float
    y: float


@dataclass
class MouseButtonEvent:
    x: float
    y: float
    button: Literal["left", "right", "middle"]
    action: Literal["down", "up"]


@dataclass
class WheelEvent:
    x: float
    y: float
    delta_x: float
    delta_y: float


@dataclass
class KeyEvent:
    key: str
    action: Literal["down", "up"]


InputEvent = MouseMoveEvent | MouseButtonEvent | WheelEvent | KeyEvent


@runtime_checkable
class LiveViewCapable(Protocol):
    """Optional capability, not a required `BrowserDriver` method -- checked
    at the composition root; drivers without it simply don't get a
    live-view route wired up."""

    async def start_screencast(
        self, ctx: ContextRef, page_id: str | None = None
    ) -> asyncio.Queue[LiveViewFrame]:
        """Returns the queue the caller reads live frames from. `page_id`
        selects which tab to screencast; `None` means the context's current
        active tab -- one screencast at a time per context, matching a real
        browser's single visible tab (see multi-tab plan's "explicitly out
        of scope")."""
        ...

    async def stop_screencast(self, ctx: ContextRef, page_id: str | None = None) -> None: ...

    async def dispatch_input(
        self, ctx: ContextRef, event: InputEvent, page_id: str | None = None
    ) -> None: ...
