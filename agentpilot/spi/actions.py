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
from typing import TYPE_CHECKING, Literal

from agentpilot.spi.artifact import ArtifactRef
from agentpilot.spi.snapshot import AXSnapshot

if TYPE_CHECKING:
    from agentpilot.spi.dom_tree import EnhancedDOMTreeNode

SnapshotEngine = Literal["aria", "fusion"]

ExtractFormat = Literal["markdown", "text", "html", "structured_data"]

# When `page.goto` considers a navigation "done". `"load"` (Playwright's own
# default) waits for every subresource, which heavy retail SPAs (Walmart,
# Amazon) with continuous ad/telemetry traffic never actually reach -- the goto
# then hangs to its timeout and surfaces as a spurious NavigationTimeout/504.
# `"domcontentloaded"` returns once the DOM is parsed; the warm-up scroll +
# dwell + extract that follow drive the rest of the render, so it's both the
# regression fix and the robust scraper default.
NavigateWaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

# The UI's anti-detection tiers that forbid any CDP `Runtime` use. Mapping the
# already-persisted `tier` (on `AgentRunCreateRequest` / `SessionOpenRequest` /
# `ScrapeRequest`) to the fusion engine's `no_runtime` flag is what makes the
# stealth mode UI-driven -- no separate request field or DB column needed.
_STEALTH_TIERS = frozenset({"basic", "stealth"})


def stealth_from_tier(tier: str) -> bool:
    """Whether the fusion engine must run Runtime-free for this UI tier.
    `basic`/`stealth` -> True (no `getEventListeners`); `enhanced`/`auto` ->
    False (full browser-use parity, Runtime allowed)."""

    return tier in _STEALTH_TIERS


@dataclass
class NavigateAction:
    url: str
    timeout_ms: int = 30_000
    wait_until: NavigateWaitUntil = "domcontentloaded"
    referer: str | None = None
    """Overrides the `Referer` header (and the `Sec-Fetch-Site` Chrome derives
    from it) for this navigation. Used by the protected scrape path to present a
    plausible organic-search landing instead of a cold, refererless deep-link.
    `None` leaves Chrome's own referer behaviour."""
    terminates_sequence: bool = True


@dataclass
class GoBackAction:
    terminates_sequence: bool = True


@dataclass
class SnapshotAction:
    viewport_only: bool = False
    max_nodes: int | None = None
    roles: tuple[str, ...] | None = None
    with_bbox: bool = False
    """Populate `SnapshotNode.bbox` for leaf refs in the resulting tree --
    extra CDP round trips per call, so opt-in (used by `agentpilot.agent`'s
    step loop; not requested by ordinary interactive-session/crawl callers)."""
    settle: bool = False
    """Wait (best-effort, bounded) for the page to reach network-idle before
    capturing the snapshot -- opt-in so only the agent's step loop pays for it
    (stable perception across steps); ordinary snapshot callers don't."""
    engine: SnapshotEngine = "aria"
    """Which perception pipeline to use. `"aria"` (default) is the battle-tested
    Playwright aria-snapshot path producing `AXSnapshot`. `"fusion"` uses the
    CDP DOM/Snapshot/Accessibility fusion engine producing an
    `EnhancedDOMTreeNode` (in `ActionResult.fused_trees`) with stable
    backendNodeId identity, element hashing, and cross-step change detection."""
    no_runtime: bool = False
    """UI-driven stealth flag (from `tier`). When set, the fusion engine skips
    every CDP `Runtime` call (`getEventListeners`), keeping Patchright's
    anti-leak posture at the cost of JS-listener-based interactivity detection."""
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
    fused_trees: list[EnhancedDOMTreeNode] = field(default_factory=list)
    """One fused `EnhancedDOMTreeNode` per `SnapshotAction(engine="fusion")` in
    the batch, index-correlated like `snapshots`. Empty on the aria path."""
    screenshots: list[bytes] = field(default_factory=list)
    extracts: list[str] = field(default_factory=list)
    js_returns: list[object] = field(default_factory=list)
    downloads: list[ArtifactRef] = field(default_factory=list)
    tabs: list[list[TabInfo]] = field(default_factory=list)
    """One entry per `ListTabsAction` in the batch (matching every other
    per-type list here being index-correlated to that action's occurrences,
    not a single running snapshot)."""
    page_title: str | None = None
    """The active page's `<title>` at the end of the batch (`page.title()`),
    populated unconditionally by the driver regardless of which `ExtractAction`
    formats were requested -- cheap, dedicated CDP getter, not tied to a full
    `page.content()` fetch. `run_ephemeral_scrape` uses this to populate
    `DocumentMetadata.title`, which was previously always `None`."""
    verifications: list[str] = field(default_factory=list)
    """Human-readable per-action outcome checks (a fill's read-back value, a
    navigation's landed URL, a click that changed the page). Lets the agent
    loop tell the model *what actually happened* instead of assuming success
    from the absence of an exception -- the driver's per-action grounding."""
    sequence_aborted: bool = False
    """Set when a prior `terminates_sequence` action changed the URL and a
    later action in the same batch would otherwise act on a stale DOM."""
    page_changed: bool = False
    """Set when a new tab is auto-focused (`_on_new_page` popup handling) or
    an unexpected navigation occurs on the active page, per the popup/download
    policy. Not set by `NewTabAction`/`SwitchTabAction` themselves -- those
    are explicit, caller-driven tab changes, not "the ground shifted under
    you" signals this field exists to carry."""
