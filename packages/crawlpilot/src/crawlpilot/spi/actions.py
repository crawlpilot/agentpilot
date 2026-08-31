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
`crawlpilot.driver.ref_cache`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode, SnapshotView

ExtractFormat = Literal["markdown", "text", "html", "structured_data"]

# When `page.goto` considers a navigation "done". `"load"` (Playwright's own
# default) waits for every subresource, which heavy retail SPAs (Walmart,
# Amazon) with continuous ad/telemetry traffic never actually reach -- the goto
# then hangs to its timeout and surfaces as a spurious NavigationTimeout/504.
# `"domcontentloaded"` returns once the DOM is parsed; the warm-up scroll +
# dwell + extract that follow drive the rest of the render, so it's both the
# regression fix and the robust scraper default.
NavigateWaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

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
    wait_until: NavigateWaitUntil = "commit"
    """`"commit"`, not the `"load"` Playwright defaults to, and not even
    `"domcontentloaded"`.

    Going back usually restores the page from the back/forward cache, and a
    bfcache restore fires `pageshow` -- it fires neither `DOMContentLoaded` nor
    `load`, because nothing is being parsed or fetched. Waiting for either means
    waiting for an event that will never come: the navigation completes
    instantly, then the call sits until its 30s timeout and raises on a
    navigation that already succeeded. `commit` is the one state a restore
    actually reaches."""
    timeout_ms: int = 30_000
    terminates_sequence: bool = True


@dataclass
class SnapshotAction:
    viewport_only: bool = False
    max_nodes: int | None = None
    roles: tuple[str, ...] | None = None
    settle: bool = False
    """Wait (best-effort, bounded) for the page to reach network-idle before
    capturing the snapshot -- opt-in so only the agent's step loop pays for it
    (stable perception across steps); ordinary snapshot callers don't."""
    selector: str | None = None
    """Scope the capture to the subtree matching this CSS selector.

    The driver resolves it over CDP (`DOM.querySelector` +
    `DOM.describeNode(depth=-1)`) into the set of backend node ids beneath the
    match, which becomes `SnapshotView.scope`. Ported from agent-browser's
    `SnapshotOptions.selector` (`snapshot.rs:216-352`), including its refusal to
    pass a non-node result on to `describeNode` -- an invalid selector, or a
    snapshot ref passed where a selector belongs, must say so rather than produce
    "Object id doesn't reference a Node"."""
    depth: int | None = None
    """Maximum indentation depth in the rendered observation."""
    no_runtime: bool = False
    """UI-driven stealth flag (from `tier`). When set, the fusion engine skips
    every CDP `Runtime` call (`getEventListeners`), keeping Patchright's
    anti-leak posture at the cost of JS-listener-based interactivity detection."""
    terminates_sequence: bool = False


@dataclass
class ExtractAction:
    """The scrape output. `markdown`/`text` route through
    `crawlpilot.extraction`'s sanitize -> convert -> post-process pipeline;
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


# --- Interaction verbs (resolve `ref` through the driver's node index) ---


@dataclass
class ClickAction:
    ref: str
    all: bool = False
    terminates_sequence: bool = False


@dataclass
class FillAction:
    ref: str
    text: str
    clear: bool = True
    """Whether to empty the field first. `clear=False` appends, which is how a
    model adds to a field it has already partly filled -- browser-use's
    `InputTextAction.clear`. With `clear=True`, `text=""` empties the field."""
    terminates_sequence: bool = False


@dataclass
class SendKeysAction:
    """A key or shortcut sent to whatever currently has focus.

    Distinct from `PressAction` in intent rather than mechanism: `press` is a
    single named key, this accepts modifier combinations (`Control+o`,
    `Shift+Tab`). Ported from browser-use's `SendKeysAction`, including its
    tolerance for the spellings models actually emit (`ctrl`, `cmd`, `mod`).
    """

    keys: str
    terminates_sequence: bool = False


@dataclass
class FindTextAction:
    """Scroll to the first occurrence of `text`.

    The one place a selector legitimately goes to the browser: `DOM.performSearch`
    is Chrome's own find-in-page, which searches text nodes the serializer may
    have truncated out of the observation. Lets a model reach content it can see
    described but not addressed (browser-use's `find_text`).
    """

    text: str
    terminates_sequence: bool = False


@dataclass
class DropdownOptionsAction:
    """List the options of a `<select>`, so a model can choose a real value
    rather than guess one and burn a step on the failure."""

    ref: str
    terminates_sequence: bool = False


@dataclass
class SearchPageAction:
    """Grep the rendered page text. Zero LLM cost, no model round trip.

    Answers "is this here, and where" without spending an observation on it --
    for verifying content exists, or locating something on a page far larger than
    the serializer's budget (browser-use's `search_page`).
    """

    pattern: str
    regex: bool = False
    case_sensitive: bool = False
    context_chars: int = 150
    max_results: int = 25
    css_scope: str | None = None
    terminates_sequence: bool = False


@dataclass
class FindElementsAction:
    """Query the DOM by CSS selector and read back tags, text and attributes.

    The structured counterpart to `search_page`: cheap bulk extraction of
    repeated structure (every row, every link) that would otherwise cost a full
    observation per page (browser-use's `find_elements`).
    """

    selector: str
    attributes: list[str] = field(default_factory=list)
    max_results: int = 50
    include_text: bool = True
    terminates_sequence: bool = False


@dataclass
class UploadFileAction:
    """Attach a local file to an `<input type=file>`.

    Wire-only, never offered to an agent -- for the same reason `execute_js` is
    not. `path` is read from the machine the driver runs on, so a model able to
    choose it could exfiltrate any file the worker can read by uploading it to a
    site it also controls. Exposing this safely needs a caller-supplied allowlist
    of paths (browser-use's `available_file_paths`), which is a session-config
    concept crawlpilot does not have yet; until it does, an integrator passes the
    path explicitly and owns that decision.
    """

    ref: str
    path: str
    terminates_sequence: bool = False


@dataclass
class DiffSnapshotAction:
    """Capture, and report what changed since the previous capture on this tab.

    The alternative is making the model diff two full observations itself, which
    costs it a page-sized read and gets the answer wrong on any page with
    repeated structure. agent-browser exposes the same verb
    (`actions.rs::handle_diff_snapshot`); the report here is the richer one the
    fusion port already produces -- NEW / REMOVED / MOVED / MODIFIED rather than
    added/removed (`crawlpilot.dom.diff`).

    The first call on a tab has nothing to compare against and says so.
    """

    settle: bool = False
    """As `SnapshotAction.settle` -- wait, bounded, for network idle first."""
    no_runtime: bool = False
    terminates_sequence: bool = False


# --- JavaScript dialogs (see `driver.dialogs`) ---
#
# Meaningful only under a `"manual"` `DialogPolicy`: under the default the page's
# dialogs are answered before any caller could see them, and `dialog_status`
# correctly reports that nothing is open.


@dataclass
class DialogStatusAction:
    """Report the dialog blocking this page, if any.

    Read-only and always safe to call, which is what makes it usable as the
    model's "did that click ask me something?" probe. Unlike accept/dismiss it
    does not raise when nothing is open -- "no dialog" is the answer, not an
    error.
    """

    terminates_sequence: bool = False


@dataclass
class DialogAcceptAction:
    """Answer the open dialog affirmatively -- OK on an `alert`, Yes on a
    `confirm`, submit on a `prompt`."""

    prompt_text: str | None = None
    """The text to submit for a `prompt`. Ignored by every other kind, and
    `None` submits the dialog's own default value."""
    terminates_sequence: bool = True
    """A dialog is usually the gate on something consequential -- a form
    submission, a navigation, a delete -- so what follows it belongs to the next
    observation, on a page that has actually moved."""


@dataclass
class DialogDismissAction:
    """Answer the open dialog negatively -- Cancel on a `confirm` or `prompt`,
    and the only available answer to an `alert`."""

    terminates_sequence: bool = True


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
    pages: float = 1.0
    """How far, in viewport-fulls. `0.5` is half a screen, `10` effectively
    reaches the end. Without it a model working down a long list had to emit one
    scroll per step and spend an observation on each (browser-use's
    `ScrollAction.pages`)."""
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
    | DiffSnapshotAction
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
    | SendKeysAction
    | FindTextAction
    | DropdownOptionsAction
    | SearchPageAction
    | FindElementsAction
    | UploadFileAction
    | DialogStatusAction
    | DialogAcceptAction
    | DialogDismissAction
    | NewTabAction
    | CloseTabAction
    | SwitchTabAction
    | ListTabsAction
)


DialogPolicy = Literal["auto_dismiss", "auto_accept", "manual"]
"""What a session does when the page opens a JavaScript dialog.

`"auto_dismiss"` is the default and reproduces Playwright's own behaviour, which
is what every caller got before dialogs were modelled at all -- an unattended
scrape must not wedge on a page that greets it with an `alert()`. `"manual"`
holds the dialog open for the caller to answer, which is what makes
`dialog_accept` / `dialog_dismiss` mean anything. See `driver.dialogs`.
"""


@dataclass(frozen=True)
class DialogInfo:
    """A JavaScript dialog the page has opened and is now blocked on.

    Lives here rather than beside the watcher in `driver.dialogs` because
    `ActionResult` carries it and `spi` may not import `driver` -- the same
    reason `ChallengeDetected` carries its verdict as a plain string.
    """

    kind: str
    """`"alert"`, `"confirm"`, `"prompt"` or `"beforeunload"`."""
    message: str
    default_value: str = ""
    """The pre-filled text of a `prompt`, empty for every other kind."""

    def describe(self) -> str:
        text = f"{self.kind} dialog: {self.message!r}"
        if self.default_value:
            text += f" (default {self.default_value!r})"
        return text


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

    fused_trees: list[EnhancedDOMTreeNode] = field(default_factory=list)
    """One fused `EnhancedDOMTreeNode` per `SnapshotAction` in the batch,
    index-correlated with the other per-type output lists."""
    snapshot_views: list[SnapshotView] = field(default_factory=list)
    """The offered-set filters for each `fused_trees` entry, index-correlated
    with it.

    The tree and the filters have to travel together because they are produced
    and consumed in different places: the driver captures the tree and is the
    only component that sees the `SnapshotAction`, while serialization happens in
    the caller. Before this, `viewport_only`, `max_nodes` and `roles` were
    declared on the HTTP boundary and read by nobody -- there was no route from
    the action to `dom.serializer`. Pass the matching entry as `serialize(...,
    view=...)`."""
    screenshots: list[bytes] = field(default_factory=list)
    extracts: list[str] = field(default_factory=list)
    js_returns: list[object] = field(default_factory=list)
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
    readouts: list[str] = field(default_factory=list)
    """One entry per read-only query action (`search_page`, `find_elements`,
    `dropdown_options`), in the order they ran.

    Separate from `verifications` because the two answer different questions: a
    verification says what an action *did*, a readout is the information the
    action was asked to *fetch*. Collapsing them would make a page search look
    like a side effect, and would leave a caller no way to distinguish grounding
    it can summarise from data it must pass through intact."""
    sequence_aborted: bool = False
    """Set when a prior `terminates_sequence` action changed the URL and a
    later action in the same batch would otherwise act on a stale DOM."""
    page_changed: bool = False
    """Set when a new tab is auto-focused (`_on_new_page` popup handling) or
    an unexpected navigation occurs on the active page, per the popup/download
    policy. Not set by `NewTabAction`/`SwitchTabAction` themselves -- those
    are explicit, caller-driven tab changes, not "the ground shifted under
    you" signals this field exists to carry."""
    status_code: int | None = None
    """HTTP status of the batch's navigation response (when one occurred), so
    callers can populate `DocumentMetadata.status_code` without a second fetch.
    `None` for batches that didn't navigate or whose response was unavailable."""
    soft_verdict: str | None = None
    """A CRAWL-scope block-detection verdict (`too_small`/`rate_limited`/
    `wrong_geo`) that did NOT raise `ChallengeDetected` -- the page rendered and
    its content is returned, but the caller may choose a cheap same-identity
    retry (Pulsar's CRAWL retry scope). `None` when the page classified OK or a
    hard PRIVACY wall was raised instead."""
    soft_weight: int = 0
    """The burn weight of `soft_verdict` (0 when none), for the session layer's
    minor-warning accounting on a retryable soft failure."""
    dialog: DialogInfo | None = None
    """A JavaScript dialog left open by this batch, under a `"manual"`
    `DialogPolicy`.

    The page is *blocked* while this is set: the dialog is why the batch stopped
    early, and nothing else will run on this tab until `dialog_accept` or
    `dialog_dismiss` answers it. Reported rather than silently dismissed because
    a `confirm()` is the page asking a question the caller may well want to
    answer "yes" to -- auto-dismissing it makes a click on "Delete account"
    report success while quietly declining on the caller's behalf."""
