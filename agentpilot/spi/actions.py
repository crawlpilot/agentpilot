"""The single interaction primitive: a closed set of tagged, driver-agnostic actions.

Adapted from Firecrawl's `actionSchema` discriminated union (convergent with
a prior internal system's own press/fill/mouseWheel/batch primitive).
`execute()` dispatches a
`list[Action]` in one round trip and returns one `ActionResult` carrying
per-type correlated output lists -- the thing that matters at millions/hour.

P0 dispatched the navigate/read/extract verbs. The interaction verbs
(Click/Fill/SelectOption/Hover/Press/Scroll) were defined from P0 for a
stable closed set -- so gateway schemas and `spi.driver.BrowserDriver` never
needed a breaking shape change -- and now dispatch for real in P1 via
`agentpilot.driver.ref_cache`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agentpilot.spi.artifact import ArtifactRef
from agentpilot.spi.snapshot import AXSnapshot

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
    """The scrape output. `markdown`/`text` route through
    `agentpilot.extraction`'s sanitize -> convert -> post-process pipeline;
    `html` returns `page.content()` raw. `base_url` is used to absolutify
    relative links/images during sanitization -- typically left unset here
    and filled in by the driver from the live page's post-navigation URL."""

    format: ExtractFormat = "markdown"
    main_content: bool = True
    include_tags: tuple[str, ...] | None = None
    exclude_tags: tuple[str, ...] | None = None
    base_url: str | None = None
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


# --- Tab management (multi-page-per-session; see driver_contract/plan.md's
# multi-tab pass). Named to match a prior internal system's agent tool
# surface (newTab/closeTab/listTabs/switchTab) rather than invented fresh.
# These mutate `PatchrightDriver`'s per-context page bookkeeping (which page
# is tracked, which is active) for *subsequent* `execute()` calls -- they do
# not retarget the page the rest of *this same* batch dispatches against,
# which stays fixed to whatever `execute()` resolved at the top from its own
# `page_id` argument. That keeps the dispatch loop's single `live` reference
# simple, and mirrors that system's `switchTab` being its own standalone
# tool call rather than something interleaved mid-sequence with page actions.


@dataclass
class NewTabAction:
    url: str | None = None
    terminates_sequence: bool = True


@dataclass
class CloseTabAction:
    page_id: str
    terminates_sequence: bool = True


@dataclass
class SwitchTabAction:
    page_id: str
    terminates_sequence: bool = True


@dataclass
class ListTabsAction:
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
    | NewTabAction
    | CloseTabAction
    | SwitchTabAction
    | ListTabsAction
)

@dataclass
class TabInfo:
    """One `ListTabsAction` entry -- mirrors a prior internal system's
    tab-listing shape (`{index, guid, title, url}`), `page_id` standing in
    for `guid`."""

    page_id: str
    url: str
    title: str
    active: bool


@dataclass
class ActionResult:
    """Per-type correlated output lists, mirroring Firecrawl's response shape."""

    snapshots: list[AXSnapshot] = field(default_factory=list)
    screenshots: list[bytes] = field(default_factory=list)
    extracts: list[str] = field(default_factory=list)
    js_returns: list[object] = field(default_factory=list)
    downloads: list[ArtifactRef] = field(default_factory=list)
    tabs: list[list[TabInfo]] = field(default_factory=list)
    """One entry per `ListTabsAction` in the batch (matching every other
    per-type list here being index-correlated to that action's occurrences,
    not a single running snapshot)."""
    sequence_aborted: bool = False
    """Set when a prior `terminates_sequence` action changed the URL and a
    later action in the same batch would otherwise act on a stale DOM."""
    page_changed: bool = False
    """Set when a new tab is auto-focused (`_on_new_page` popup handling) or
    an unexpected navigation occurs on the active page, per the popup/download
    policy. Not set by `NewTabAction`/`SwitchTabAction` themselves -- those
    are explicit, caller-driven tab changes, not "the ground shifted under
    you" signals this field exists to carry."""
