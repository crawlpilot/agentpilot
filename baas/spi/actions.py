"""The single interaction primitive: a closed set of tagged, driver-agnostic actions.

Adapted from Firecrawl's `actionSchema` discriminated union (convergent with
Browser4's press/fill/mouseWheel/batch backport). `execute()` dispatches a
`list[Action]` in one round trip and returns one `ActionResult` carrying
per-type correlated output lists -- the thing that matters at millions/hour.

P0 dispatched the navigate/read/extract verbs. The interaction verbs
(Click/Fill/SelectOption/Hover/Press/Scroll) were defined from P0 for a
stable closed set -- so gateway schemas and `spi.driver.BrowserDriver` never
needed a breaking shape change -- and now dispatch for real in P1 via
`baas.driver.ref_cache`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from baas.spi.artifact import ArtifactRef
from baas.spi.snapshot import AXSnapshot

ExtractFormat = Literal["markdown", "text", "html"]


@dataclass
class NavigateAction:
    url: str
    timeout_ms: int = 30_000
    terminates_sequence: bool = True


@dataclass
class GoBackAction:
    terminates_sequence: bool = True


@dataclass
class SnapshotAction:
    viewport_only: bool = False
    max_nodes: int | None = None
    roles: tuple[str, ...] | None = None
    terminates_sequence: bool = False


@dataclass
class ExtractAction:
    """The scrape output. `markdown`/`text` route through `baas.extraction`
    (trafilatura); `html` returns `page.content()` raw."""

    format: ExtractFormat = "markdown"
    main_content: bool = True
    terminates_sequence: bool = False


@dataclass
class ScreenshotAction:
    full_page: bool = False
    terminates_sequence: bool = False


@dataclass
class WaitAction:
    ms: int | None = None
    ref: str | None = None
    terminates_sequence: bool = False


@dataclass
class ExecuteJsAction:
    script: str
    terminates_sequence: bool = False


# --- Interaction verbs (P1: need spi.driver.ref_cache to resolve `ref`) ---


@dataclass
class ClickAction:
    ref: str
    all: bool = False
    terminates_sequence: bool = False


@dataclass
class FillAction:
    ref: str
    text: str
    terminates_sequence: bool = False


@dataclass
class SelectOptionAction:
    ref: str
    values: list[str] = field(default_factory=list)
    terminates_sequence: bool = False


@dataclass
class HoverAction:
    ref: str
    terminates_sequence: bool = False


@dataclass
class PressAction:
    key: str
    terminates_sequence: bool = False


@dataclass
class ScrollAction:
    direction: Literal["up", "down", "left", "right"]
    ref: str | None = None
    terminates_sequence: bool = False


Action = (
    NavigateAction
    | GoBackAction
    | SnapshotAction
    | ExtractAction
    | ScreenshotAction
    | WaitAction
    | ExecuteJsAction
    | ClickAction
    | FillAction
    | SelectOptionAction
    | HoverAction
    | PressAction
    | ScrollAction
)

@dataclass
class ActionResult:
    """Per-type correlated output lists, mirroring Firecrawl's response shape."""

    snapshots: list[AXSnapshot] = field(default_factory=list)
    screenshots: list[bytes] = field(default_factory=list)
    extracts: list[str] = field(default_factory=list)
    js_returns: list[object] = field(default_factory=list)
    downloads: list[ArtifactRef] = field(default_factory=list)
    sequence_aborted: bool = False
    """Set when a prior `terminates_sequence` action changed the URL and a
    later action in the same batch would otherwise act on a stale DOM."""
    page_changed: bool = False
    """Set on popup adoption (new page becomes "the page") or unexpected
    navigation, per the popup/download policy."""
