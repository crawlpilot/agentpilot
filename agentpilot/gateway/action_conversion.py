"""`ActionIn` (the Pydantic, HTTP-boundary discriminated union) -> `spi.actions
.Action` (the driver-agnostic dataclass union) -- shared by `routes/sessions.py`
(`/v1/sessions/{id}/execute`'s caller-supplied batch) and `routes/scrape.py`
(`/v1/scrape`'s server-composed batch), so both build the exact same
`spi.actions.Action` values from the exact same wire shape rather than two
routes drifting into slightly different conversions over time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentpilot.gateway.schemas import (
    ActionIn,
    ClickActionIn,
    CloseTabActionIn,
    ExecuteJsActionIn,
    ExtractActionIn,
    FillActionIn,
    GoBackActionIn,
    HoverActionIn,
    ListTabsActionIn,
    NavigateActionIn,
    NewTabActionIn,
    PressActionIn,
    ScreenshotActionIn,
    ScrollActionIn,
    SelectOptionActionIn,
    SnapshotActionIn,
    SwitchTabActionIn,
    WaitActionIn,
)
from agentpilot.spi import actions as spi_actions

_ACTION_CONVERTERS: dict[type, Callable[[Any], spi_actions.Action]] = {
    NavigateActionIn: lambda a: spi_actions.NavigateAction(url=a.url, timeout_ms=a.timeout_ms),
    GoBackActionIn: lambda a: spi_actions.GoBackAction(),
    SnapshotActionIn: lambda a: spi_actions.SnapshotAction(
        viewport_only=a.viewport_only,
        max_nodes=a.max_nodes,
        roles=tuple(a.roles) if a.roles is not None else None,
    ),
    ExtractActionIn: lambda a: spi_actions.ExtractAction(
        format=a.format, main_content=a.main_content
    ),
    ScreenshotActionIn: lambda a: spi_actions.ScreenshotAction(full_page=a.full_page),
    WaitActionIn: lambda a: spi_actions.WaitAction(ms=a.ms, ref=a.ref),
    ExecuteJsActionIn: lambda a: spi_actions.ExecuteJsAction(script=a.script),
    ClickActionIn: lambda a: spi_actions.ClickAction(ref=a.ref, all=a.all),
    FillActionIn: lambda a: spi_actions.FillAction(ref=a.ref, text=a.text),
    SelectOptionActionIn: lambda a: spi_actions.SelectOptionAction(ref=a.ref, values=a.values),
    HoverActionIn: lambda a: spi_actions.HoverAction(ref=a.ref),
    PressActionIn: lambda a: spi_actions.PressAction(key=a.key),
    ScrollActionIn: lambda a: spi_actions.ScrollAction(direction=a.direction, ref=a.ref),
    NewTabActionIn: lambda a: spi_actions.NewTabAction(url=a.url),
    CloseTabActionIn: lambda a: spi_actions.CloseTabAction(page_id=a.page_id),
    SwitchTabActionIn: lambda a: spi_actions.SwitchTabAction(page_id=a.page_id),
    ListTabsActionIn: lambda a: spi_actions.ListTabsAction(),
}


def to_spi_action(action_in: ActionIn) -> spi_actions.Action:
    return _ACTION_CONVERTERS[type(action_in)](action_in)
