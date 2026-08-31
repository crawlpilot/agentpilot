"""The one concrete `BrowserDriver` implementation.

Playwright/Patchright objects never leave this module -- everything returned
to callers is a `crawlpilot.spi` dataclass. `execute()` is the single dispatch loop
batching a whole `list[Action]` into one `ActionResult`. P1 adds real
dispatch for the interaction verbs (via `driver/ref_cache.py`), snapshot
token-budget filtering (`roles`/`max_nodes`/`viewport_only`), and the
view-only live-view screencast (`LiveViewCapable`, at the bottom of this
class). This pass adds multi-tab: one `_Context` per `ContextRef` now owns a
`dict[page_id, _Page]` (was a single implicit page) -- see the multi-tab plan
for how this mirrors a prior internal system's tab/driver-pool/multi-driver
container shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, assert_never, cast

import httpx
import structlog

# Not re-exported from `patchright.async_api` (unlike upstream Playwright,
# which does publicly expose this) -- observed directly: a page/context that
# closed for a reason other than a renderer crash (e.g. torn down mid-poll,
# or a race between a tab close and an in-flight action) previously surfaced
# as a raw "TargetClosedError: ... has been closed" 500, not a typed error,
# since only the "crash" event was ever wired up. Importing from the private
# module is the pragmatic tradeoff here over not catching this at all.
from patchright._impl._errors import TargetClosedError
from patchright.async_api import (
    BrowserContext,
    CDPSession,
    Page,
    ProxySettings,
)
from patchright.async_api import StorageState as PlaywrightStorageState
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from crawlpilot import metrics
from crawlpilot.config import LaunchConfig
from crawlpilot.dom.diff import diff_snapshots, render_change_block
from crawlpilot.driver import browser_discovery, cdp_element, dialogs, humanize, mouse, warmup
from crawlpilot.driver.dialogs import DialogInterrupt, DialogWatcher, GuardedSession
from crawlpilot.driver.dom_fusion_engine import capture_fused_tree
from crawlpilot.driver.live_view import (
    SCREENCAST_START_PARAMS,
    parse_screencast_frame,
    to_cdp_input_params,
)
from crawlpilot.driver.node_index import NodeIndex
from crawlpilot.driver.process_launcher import ProcessLauncher
from crawlpilot.egress.policy import apply_baseline
from crawlpilot.extensions.mounts import BlockHooks
from crawlpilot.extraction import block_detect
from crawlpilot.extraction.extractor import extract
from crawlpilot.spi.actions import (
    Action,
    ActionResult,
    ClickAction,
    CloseTabAction,
    DialogAcceptAction,
    DialogDismissAction,
    DialogInfo,
    DialogPolicy,
    DialogStatusAction,
    DiffSnapshotAction,
    DropdownOptionsAction,
    ExecuteJsAction,
    ExtractAction,
    FillAction,
    FindElementsAction,
    FindTextAction,
    GoBackAction,
    HoverAction,
    ListTabsAction,
    NavigateAction,
    NewTabAction,
    PressAction,
    ScreenshotAction,
    ScrollAction,
    SearchPageAction,
    SelectOptionAction,
    SendKeysAction,
    SnapshotAction,
    SwitchTabAction,
    TabInfo,
    UploadFileAction,
    WaitAction,
)
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode, SnapshotView
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import (
    CapacityExhausted,
    ChallengeDetected,
    ContextCrashed,
    NavigationTimeout,
    SelectorNotFound,
    StaleRefError,
    TabNotFound,
)
from crawlpilot.spi.geometry import BoundingBox
from crawlpilot.spi.health import ContextHealth, HealthStatus
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState
from crawlpilot.spi.proxy import ProxyEndpoint
from crawlpilot.spi.storage_state import LocalStorageEntry, OriginState, StorageState
from crawlpilot.spi.streaming import InputEvent, LiveViewFrame

log = structlog.get_logger(__name__)

_REF_CONSUMING = (
    ClickAction,
    FillAction,
    SelectOptionAction,
    HoverAction,
    DropdownOptionsAction,
    UploadFileAction,
)
"""Actions carrying a mandatory `ref`, so the batch loop knows to abort them once
an earlier action has invalidated the refs it would use. `WaitAction`/
`ScrollAction` are checked separately -- their `ref` is optional."""

def _collect_backend_ids(node: dict[str, Any], into: set[int]) -> None:
    """Every `backendNodeId` in a `DOM.describeNode(depth=-1)` payload.

    Walks `children`, `shadowRoots` and `contentDocument` alike -- a selector
    that matches a component wrapper should scope to what the user sees inside
    it, which for a web component is behind its shadow root. Mirrors
    agent-browser's `collect_backend_node_ids` (`snapshot.rs:1338`).
    """

    backend_id = node.get("backendNodeId")
    if isinstance(backend_id, int):
        into.add(backend_id)
    for key in ("children", "shadowRoots", "pseudoElements"):
        for child in node.get(key) or ():
            _collect_backend_ids(child, into)
    content = node.get("contentDocument")
    if content:
        _collect_backend_ids(content, into)


_DIALOG_ACTIONS = (DialogStatusAction, DialogAcceptAction, DialogDismissAction)
"""The only actions that may run while a dialog holds the renderer. Everything
else would block on it, so `_dispatch` refuses them with `DialogInterrupt`
rather than letting the call hang to its timeout."""

# Interactive actions that get a human `gap` pause *before* them when they
# follow another action in the same batch -- the between-actions dwell Pulsar
# applies (InteractSettings `gap`). Navigate/Extract/Snapshot/Wait are excluded
# so a plain scrape batch (Navigate -> Extract) pays no extra latency; only
# genuine interaction sequences (click, fill, press, scroll, hover, select) are
# paced.
_GAP_BEFORE = (
    ClickAction,
    FillAction,
    SelectOptionAction,
    HoverAction,
    PressAction,
    SendKeysAction,
    ScrollAction,
)

# Parameters arrive as one argument object rather than interpolated into the
# source: the pattern and selector come from a model, and a quote or backslash
# in either would otherwise break out of the script.
_SEARCH_PAGE_JS = """(opts) => {
    const scope = opts.cssScope ? document.querySelector(opts.cssScope) : document.body;
    if (!scope) return {matches: [], total: 0};
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    const parts = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) parts.push(n.nodeValue);
    const text = parts.join(' ').replace(/\\s+/g, ' ');

    const flags = opts.caseSensitive ? 'g' : 'gi';
    const source = opts.regex
        ? opts.pattern
        : opts.pattern.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
    let re;
    try { re = new RegExp(source, flags); } catch (e) { return {error: String(e)}; }

    const matches = [];
    let total = 0;
    for (let m = re.exec(text); m !== null; m = re.exec(text)) {
        total += 1;
        if (matches.length < opts.maxResults) {
            const from = Math.max(0, m.index - opts.contextChars);
            const to = Math.min(text.length, m.index + m[0].length + opts.contextChars);
            matches.push(text.slice(from, to));
        }
        if (m[0] === '') re.lastIndex += 1;  // zero-width match would loop forever
        if (total > 5000) break;
    }
    return {matches, total};
}"""

_FIND_ELEMENTS_JS = """(opts) => {
    let nodes;
    try { nodes = document.querySelectorAll(opts.selector); }
    catch (e) { return {error: String(e)}; }
    const elements = [];
    for (const el of Array.from(nodes).slice(0, opts.maxResults)) {
        const attrs = {};
        for (const name of opts.attributes) {
            // Read href/src off the property so the value is the absolute URL a
            // caller can actually use, not the raw relative attribute.
            const value = (name === 'href' || name === 'src') && el[name] != null
                ? el[name]
                : el.getAttribute(name);
            if (value != null) attrs[name] = String(value).slice(0, 500);
        }
        elements.push({
            tag: el.tagName.toLowerCase(),
            text: opts.includeText
                ? (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300)
                : '',
            attrs,
        });
    }
    return {elements, total: nodes.length};
}"""


def _render_dropdown_options(ref: str, options: list[dict[str, str]]) -> str:
    if not options:
        return f"dropdown_options: {ref} has no <option> children (is it a real <select>?)"
    lines = [f"dropdown_options for {ref}:"]
    lines += [f"  {opt['text']!r} (value={opt['value']!r})" for opt in options]
    return "\n".join(lines)


_LIVENESS_TIMEOUT_S = 2.0
"""Upper bound on the CDP liveness/keepalive ping. A responsive context answers
`Page.getLayoutMetrics` in well under this; a wedged renderer hits the bound and
is reported dead so the session layer can reopen it, rather than hanging the
acquire path on an unresponsive Chrome."""

_CLICK_NAVIGATION_TIMEOUT_MS = 5_000
"""How long a click waits for a navigation it triggered to parse a document.

Longer than `_SETTLE_TIMEOUT_MS` because this is a real page load rather than a
quiet-network check, and short enough that a click which merely *looked* like a
navigation does not stall the batch."""

_SETTLE_TIMEOUT_MS = 1_500
"""Upper bound on the best-effort network-idle wait before an opt-in
(`SnapshotAction.settle`) snapshot. Bounded because `networkidle` never fires
on pages with persistent connections (long-poll, websockets, analytics
beacons); on those the wait simply caps out and the snapshot proceeds. A
MutationObserver-based probe that returns early on DOM quiescence (Browser4's
`waitForDOMSettle`) is a later refinement -- this is the cheap first cut."""

_SCROLL_UNIT: dict[str, tuple[float, float]] = {
    "down": (0.0, 1.0),
    "up": (0.0, -1.0),
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
}
"""Direction as a unit vector. The distance is `pages` multiplied by the size of
whatever is being scrolled -- the viewport, or the element's own box -- rather
than the flat 600px this used to be, so "one page" means a screenful on a
laptop and on a 4K display alike."""

# --- Behavioral-realism knobs (anti-detection). Real users don't fill an
# input in a single instantaneous DOM write, teleport the pointer to an
# element's exact center, or scroll by a pixel-perfect fixed delta every
# time -- WAFs that score pointer/keystroke telemetry flag exactly that
# regularity. These add human-plausible variance without changing what an
# action ultimately does. All keyed off `random`, seeded per-process, so
# tests that need determinism can `random.seed()`.


_SCROLL_JITTER_FRAC = 0.15
"""Fraction of the base scroll delta to randomize by (±), so repeated
scrolls aren't a pixel-identical fixed wheel event."""


def _navigation_leak_reason(status: int | None) -> str | None:
    """Pure classifier for navigation-response leak signals -- a site telling
    us it suspects a bot. Status-only for now (no extra round trip); a
    content/title-based captcha check (Cloudflare "Just a moment", hCaptcha)
    is a later refinement that would cost a `content()` fetch. Feeds
    `_ContextHealth.leak_warnings`, which a later pass uses to rotate the
    profile (mirrors Browser4's privacy-leak-driven context drop)."""

    if status == 403:
        return "http_403"
    if status == 429:
        return "http_429"
    return None


def _jitter(value: float) -> float:
    return value * (1 + random.uniform(-_SCROLL_JITTER_FRAC, _SCROLL_JITTER_FRAC))


def _launch_target(launch: browser_discovery.Launch) -> dict[str, str]:
    """The one launch kwarg that names the browser.

    `channel` and `executable_path` are mutually exclusive in Playwright --
    passing both is an error rather than a preference — so `Launch` carries
    exactly one and this turns it into the right kwarg.
    """

    if launch.channel is not None:
        return {"channel": launch.channel}
    assert launch.executable_path is not None
    return {"executable_path": launch.executable_path}


async def _resolve_cdp_url(url: str) -> str:
    """Turn a CDP endpoint into something `connect_over_cdp` accepts.

    A `ws://` URL is already the websocket. An `http://` one is the discovery
    endpoint, and Playwright can take it directly -- but resolving
    `/json/version` ourselves gives a clear, early error naming the URL when
    nothing is listening, instead of a timeout deep inside the connect.
    """

    if url.startswith("ws://") or url.startswith("wss://"):
        return url

    base = url.rstrip("/")
    version = base if base.endswith("/json/version") else f"{base}/json/version"
    host = httpx.URL(version).host
    try:
        # `trust_env=False` for loopback: an ambient HTTP_PROXY would otherwise
        # send a request for 127.0.0.1 through a proxy that cannot route it.
        local = host in ("localhost", "127.0.0.1", "::1")
        async with httpx.AsyncClient(timeout=5.0, trust_env=not local) as client:
            payload = (await client.get(version)).json()
    except Exception as exc:
        raise ContextCrashed(
            f"no browser answering at {url!r}: {exc}. Check the browser is "
            "running and its remote-debugging port is reachable from here."
        ) from exc

    websocket = payload.get("webSocketDebuggerUrl")
    if not websocket:
        raise ContextCrashed(f"{version} returned no webSocketDebuggerUrl: {payload!r}")
    return str(websocket)


def _scroll_delta(
    direction: str, pages: float, width: float, height: float
) -> tuple[float, float]:
    """Wheel delta for `pages` screenfuls in `direction`, jittered.

    The jitter is behavioural: a wheel event whose delta is the same round number
    every time is not something a physical wheel or trackpad produces.
    """

    ux, uy = _SCROLL_UNIT[direction]
    return _jitter(ux * pages * width), _jitter(uy * pages * height)


DEFAULT_MAX_TABS_PER_SESSION = 10
"""Per-session tab cap (`_Context.max_tabs`). A prior internal system's
equivalent driver-pool capacity hard-ceilings much higher, but that governs a
*shared driver pool* spread across many crawls; one agentpilot session
is already a dedicated Chrome context per tenant identity, a heavier unit, so
a smaller default is the right translation, not a straight copy of the
number. Configurable via `AGENTPILOT_MAX_TABS_PER_SESSION` (see `wiring.py`)."""


@dataclass
class _Page:
    """One tab's live state -- was `_Live` pre-multi-tab, minus the fields
    that are actually context-wide (`context`, `alive`/`death_reason` for the
    whole browser, `block_popups`), which moved to `_Context` below."""

    page: Page
    epoch: int = 0
    alive: bool = True
    death_reason: str | None = None
    cdp_session: CDPSession | None = None
    frame_queue: asyncio.Queue[LiveViewFrame] | None = None
    frame_queue_refs: int = 0
    """Count of live-view websocket connections currently sharing
    `frame_queue` -- see `start_screencast`/`stop_screencast`."""
    nodes: NodeIndex = field(default_factory=NodeIndex)
    """`ref -> captured node` from the most recent snapshot."""
    last_tree: EnhancedDOMTreeNode | None = None
    """The most recent capture, kept so `DiffSnapshotAction` has something to
    compare against. Per tab, like `nodes`: a diff across two different pages
    would report every element as new and be worse than useless."""
    dialogs: DialogWatcher = field(default_factory=DialogWatcher)
    """This tab's JavaScript-dialog state.

    Per tab, not per context: a dialog blocks the renderer of the page that
    opened it, and a background tab's `confirm()` must not abort an action on
    the foreground one (agent-browser filters the same way by session,
    `interaction.rs:44-46`)."""
    frame_sessions: dict[str, CDPSession] = field(default_factory=dict)
    """CDP frame id -> that frame's own session, for cross-origin iframes.

    Held for the life of the tab rather than per snapshot: the same sessions
    capture the frame's DOM *and* receive its input events, and a node captured
    in one is only addressable in that same one."""


@dataclass
class _ContextHealth:
    """Per-context health tallies feeding `session.rotation.should_retire`, which
    rotates a flagged profile (mirrors Browser4's `AbstractPrivacyContext`:
    `privacyLeakWarnings`, `failureRate`, `smallPageRate`). A "task" here is one
    `execute()` batch."""

    tasks: int = 0
    successes: int = 0
    failures: int = 0
    small_pages: int = 0
    """Batches that returned a suspiciously small/blocked page.

    Read by `should_retire` but never incremented -- the quality-detection pass
    that would populate it does not exist, so this rung of the rotation policy is
    currently inert. Kept because the threshold that reads it is real and the
    signal is the one Browser4 rotates on; noted here because a zero that is
    never written looks like a healthy context rather than an unmeasured one."""
    leak_warnings: int = 0
    """Weighted bot-detection signals (captcha/challenge/block), accrued by
    `_post_navigate` and tripping rotation at `RotationThresholds
    .max_leak_warnings`."""

    @property
    def failure_rate(self) -> float:
        return self.failures / self.tasks if self.tasks else 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.tasks if self.tasks else 0.0


@dataclass
class _Context:
    """One `ContextRef`'s live state -- the per-context wrapper `_Live` used
    to be (a context WAS a page, 1:1). Mirrors a prior internal system's
    multi-driver container: `pages` is its driver dict, `active_page_id` is
    its front-driver pointer."""

    context: BrowserContext
    pages: dict[str, _Page]
    active_page_id: str
    remote_browser: Any = None
    """The `Browser` returned by `connect_over_cdp`, when this context attached
    to a browser we did not launch.

    Held so `close()` can disconnect the websocket without *killing the browser*:
    a locally-launched context owns its Chrome and closing it should end the
    process, but a remote one is shared -- terminating someone else's browser
    because one client finished would be a surprising thing for a library to do."""
    max_tabs: int = DEFAULT_MAX_TABS_PER_SESSION
    alive: bool = True
    death_reason: str | None = None
    block_popups: bool = False
    cdp_http_base: str | None = None
    """Local (loopback-only) CDP HTTP base, e.g. `'http://127.0.0.1:9222'`,
    set only when this context was opened with `enable_cdp=True` -- see
    `CdpEndpointCapable` at the bottom of this class."""
    warmup: bool = False
    """Opt-in per-open flag (Akamai/protected scrapes): run the human warm-up
    routine after each navigation. Off for interactive/test callers."""
    detect_blocks: bool = False
    """Opt-in per-open flag: inspect the navigated page body and raise
    `ChallengeDetected` on a bot wall (incl. HTTP-200 Access Denied). Off by
    default so the general driver never pays the extra `content()` fetch."""
    page_changed: bool = False
    """Set when `_on_new_page` auto-focuses a new tab (see multi-tab plan's
    "New-tab focus" decision: real-browser semantics, matching a prior
    internal system's unconditional new-window auto-follow). Read once per
    `execute()` call and reset, same lifecycle as before."""
    health: _ContextHealth = field(default_factory=_ContextHealth)
    """Per-context health tallies (Wave 0 instrumentation) -- see
    `_ContextHealth`. Updated in `execute()`; not yet consumed by any policy."""
    delay_policy: humanize.DelayPolicy = humanize.DEFAULT
    """The human-timing preset (`humanize.DelayPolicy`) this context's
    interactions sample from -- click/fill/type keystroke delays and the
    between-actions `gap`. Set from the scrape tier at `open()` (protected tiers
    get STEALTH), so the tier actually modulates interaction cadence rather than
    every context using one hardcoded set of ranges."""
    dialog_policy: DialogPolicy = dialogs.DEFAULT_POLICY
    """How this context answers JavaScript dialogs -- see `driver.dialogs`.

    Context-wide rather than per tab because it is a caller's standing choice,
    not a property of a page: an agent loop wants every tab it opens to hold its
    dialogs, and a scrape wants every tab to dismiss them. Applied to each
    `_Page` as it is wired."""
    expecting_explicit_tab: bool = False
    """Set around `_create_tab`'s own `context.new_page()` call.
    `new_page()` fires the exact same context-level `"page"` event an
    organic popup does (confirmed empirically: the handler runs and
    registers the page *before* `new_page()`'s own await even resolves) --
    without this flag, `_on_new_page` and `_create_tab` would each mint a
    separate `_Page` for the same underlying Playwright `Page`, which is
    exactly what caused "clicking + once opens two identical tabs"."""


def _find_free_port() -> int:
    """Picks our own ephemeral port up front, rather than passing
    `--remote-debugging-port=0` and discovering what Chrome chose after the
    fact -- avoids coupling to Chrome's `DevToolsActivePort` file format
    (its one alternative discovery mechanism; parsing subprocess stderr, the
    other alternative, isn't an option since `launch_persistent_context`
    doesn't surface the child process's stdout/stderr to Python code at
    all). Same idiom the `browser-use` project's own local launcher uses.
    Accepts the standard, small TOCTOU race between closing this socket and
    Chrome binding the same port -- a non-issue in this single-process-per-
    worker-container setup."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def _wait_for_cdp_ready(port: int, timeout: float = 5.0) -> None:
    """Polls Chrome's own DevTools HTTP server rather than just the TCP port
    accepting connections -- a bound port doesn't guarantee the `/json/
    version` endpoint is actually serving yet. Mirrors `browser-use`'s own
    `_wait_for_cdp_url()` polling loop."""

    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"CDP debug port {port} did not become ready within {timeout}s")


def _proxy_settings(proxy: ProxyEndpoint | None) -> ProxySettings | None:
    if proxy is None:
        return None
    settings: ProxySettings = {"server": f"{proxy.scheme}://{proxy.host}:{proxy.port}"}
    if proxy.username:
        settings["username"] = proxy.username
    if proxy.password:
        settings["password"] = proxy.password
    return settings


def _to_playwright_storage_state(state: StorageState) -> PlaywrightStorageState:
    return cast(
        PlaywrightStorageState,
        {
            "cookies": state.cookies,
            "origins": [
                {
                    "origin": origin.origin,
                    "localStorage": [
                        {"name": entry.name, "value": entry.value} for entry in origin.local_storage
                    ],
                }
                for origin in state.origins
            ],
        },
    )


def _from_playwright_storage_state(raw: Mapping[str, Any]) -> StorageState:
    origins = [
        OriginState(
            origin=o["origin"],
            local_storage=[
                LocalStorageEntry(name=e["name"], value=e["value"])
                for e in o.get("localStorage", [])
            ],
        )
        for o in raw.get("origins", [])
    ]
    return StorageState(cookies=list(raw.get("cookies", [])), origins=origins)


class PatchrightDriver:
    def __init__(
        self,
        launcher: ProcessLauncher,
        max_tabs_per_session: int = DEFAULT_MAX_TABS_PER_SESSION,
        node_id: str = "local",
        block_hooks: BlockHooks | None = None,
        cross_origin_iframes: bool = True,
        launch: LaunchConfig | None = None,
    ) -> None:
        self._launcher = launcher
        self._max_tabs_per_session = max_tabs_per_session
        self._node_id = node_id
        self._launch = launch or LaunchConfig()
        """Which browser to drive, and whether to launch one at all. Defaults to
        "work it out" -- see `driver.browser_discovery`."""
        # Per-site block detection is contributed by extensions now, not
        # auto-installed at import (plan D12). `None` means the generic
        # classifier only -- correct for a caller that registered nothing.
        self._block_hooks = block_hooks
        self._cross_origin_iframes = cross_origin_iframes
        """Whether a snapshot also captures cross-origin iframe content from
        each frame's own CDP target (browser-use's `cross_origin_iframes`).

        On by default: without it, everything inside an embedded payment field,
        consent banner or third-party widget is simply absent from what the agent
        can see, and no amount of retrying finds it. Off is the cheap path -- one
        target, no per-frame attach -- for a crawl that only wants the top
        document."""
        self._contexts: dict[str, _Context] = {}

    def _require_context(self, ctx: ContextRef) -> _Context:
        cctx = self._contexts.get(ctx.context_id)
        if cctx is None or not cctx.alive:
            raise ContextCrashed(f"context {ctx.context_id} is not alive")
        return cctx

    def _require_page(self, cctx: _Context, page_id: str | None) -> _Page:
        pid = page_id or cctx.active_page_id
        live = cctx.pages.get(pid)
        if live is None:
            raise TabNotFound(pid)
        if not live.alive:
            raise ContextCrashed(f"tab {pid} is not alive: {live.death_reason}")
        return live

    async def open(
        self,
        identity: IdentityRef,
        profile_dir: Path,
        proxy: ProxyEndpoint | None,
        headful: bool,
        egress: EgressPolicy,
        block_popups: bool = False,
        enable_cdp: bool = False,
        dialog_policy: DialogPolicy = dialogs.DEFAULT_POLICY,
        locale: str | None = None,
        timezone_id: str | None = None,
        warmup: bool = False,
        detect_blocks: bool = False,
        user_agent: str | None = None,
        init_script: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        extra_launch_args: list[str] | None = None,
        interact_profile: str | None = None,
        block_resource_types: tuple[str, ...] | None = None,
        block_hosts: tuple[str, ...] | None = None,
    ) -> ContextRef:
        apply_baseline(egress)
        if headful:
            self._launcher.ensure_xvfb()

        # Chrome's own `Singleton{Lock,Cookie,Socket}` guard against two
        # live processes sharing one profile dir concurrently -- redundant
        # here, since `Registry.acquire()` already gives each identity
        # exclusive ownership of its `profile_dir` fleet-wide, so nothing
        # legitimate is ever running against it when `open()` is called.
        # Left behind by a process that never exited gracefully (a worker
        # container recreated under a new hostname, a hard kill), the lock
        # symlink's target hostname never matches again -- Chrome then
        # refuses to launch ("profile appears to be in use by another
        # process") forever, since nothing else ever clears it. Observed
        # directly: several `profile_dir`s in the dev fleet permanently
        # wedged this way after a worker container outlived the hostname
        # its profiles were locked under.
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (profile_dir / name).unlink(missing_ok=True)

        cdp_port: int | None = None
        launch_args: list[str] = []
        if enable_cdp:
            cdp_port = _find_free_port()
            # Additive to Playwright/Patchright's own private
            # `--remote-debugging-pipe` control channel, not a replacement --
            # Chrome accepts both simultaneously. Loopback-only by default
            # (never pass `--remote-debugging-address`); no
            # `--remote-allow-origins` either, since our own relay connects
            # via the `websockets` library, which sends no `Origin` header,
            # so Chrome's origin-check middleware (which only blocks WS
            # upgrades carrying a *mismatched* `Origin`) never triggers.
            launch_args.append(f"--remote-debugging-port={cdp_port}")

        if extra_launch_args:
            # Curated hardening flags (window geometry + suppressed background
            # chatter) from the pinned fingerprint. Additive to Patchright's own
            # defaults; we never pass `--disable-blink-features` here so
            # Patchright still injects its AutomationControlled patch.
            launch_args.extend(extra_launch_args)

        # Only pass locale/timezone through when the caller actually set
        # them -- Playwright treats an explicit `None` and an omitted kwarg
        # differently, and omitting keeps Chrome's own defaults for the
        # interactive/test callers that don't care (see `BrowserDriver.open`).
        context_kwargs: dict[str, Any] = {}
        if locale is not None:
            context_kwargs["locale"] = locale
        if timezone_id is not None:
            context_kwargs["timezone_id"] = timezone_id
        if user_agent is not None:
            context_kwargs["user_agent"] = user_agent

        # Headful is only honoured when a display is actually available (Xvfb on
        # the Linux worker, or an exported DISPLAY). On a dev machine or a
        # display-less container it degrades to headless rather than failing to
        # launch -- the enhanced tier still keeps its fingerprint + warm-up, it
        # just can't add the (headful-only) OS-level input path yet.
        # An explicit `headless=` on the launch config wins over the per-open
        # `headful` flag: the caller configured the browser, the session layer
        # only expressed a preference. `None` leaves the preference intact,
        # which is what makes it a tri-state rather than a default of False.
        want_headful = headful if self._launch.headless is None else not self._launch.headless
        effective_headful = want_headful and self._launcher.ensure_display()
        if want_headful and not effective_headful:
            log.info("driver.headful_downgraded_no_display", context=str(profile_dir))

        playwright = await self._launcher.get_playwright()
        remote = None
        if self._launch.cdp_url:
            context, remote = await self._attach_remote(playwright, context_kwargs)
        else:
            launch = browser_discovery.resolve_browser(
                executable_path=self._launch.executable_path,
                channel=self._launch.channel,
            )
            log.info(
                "driver.browser_resolved",
                source=launch.source,
                channel=launch.channel,
                executable=launch.executable_path,
            )
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=not effective_headful,
                no_viewport=True,
                proxy=_proxy_settings(proxy),
                args=launch_args,
                **_launch_target(launch),
                **context_kwargs,
            )
        if init_script is not None:
            # Context-level: applies to every page (current + future) before any
            # page script runs -- the pinned per-identity fingerprint's
            # navigator/WebGL/language patches (see identity/fingerprint.py).
            await context.add_init_script(init_script)
        if extra_http_headers:
            # Pin the always-sent Client-Hint headers (Sec-CH-UA*) to the same
            # Chrome build as `user_agent` + navigator.userAgentData, so the
            # wire-level hints agree with the spoofed UA rather than leaking the
            # real (newer) Chrome the header would otherwise carry.
            await context.set_extra_http_headers(extra_http_headers)
        if block_resource_types or block_hosts:
            await self._install_resource_blocking(
                context, tuple(block_resource_types or ()), tuple(block_hosts or ())
            )
        if enable_cdp and remote is None:
            assert cdp_port is not None
            await _wait_for_cdp_ready(cdp_port)
        page = context.pages[0] if context.pages else await context.new_page()

        context_id = str(uuid.uuid4())
        page_id = str(uuid.uuid4())
        cctx = _Context(
            context=context,
            remote_browser=remote,
            pages={page_id: _Page(page=page)},
            active_page_id=page_id,
            max_tabs=self._max_tabs_per_session,
            block_popups=block_popups,
            dialog_policy=dialog_policy,
            # A remote browser already has whatever debugging endpoint its owner
            # gave it; we did not open one and must not advertise ours.
            cdp_http_base=(
                self._launch.cdp_url
                if remote is not None
                else (f"http://127.0.0.1:{cdp_port}" if enable_cdp else None)
            ),
            warmup=warmup,
            detect_blocks=detect_blocks,
            delay_policy=(
                humanize.by_name(interact_profile) if interact_profile else humanize.DEFAULT
            ),
        )

        def _wire_page(pid: str, pg: Page) -> None:
            def _on_crash(_page: Page) -> None:
                live_page = cctx.pages.get(pid)
                if live_page is not None:
                    live_page.alive = False
                    live_page.death_reason = "page_crash"
                log.warning("driver.page_crashed", context_id=context_id, page_id=pid)

            def _on_close(_page: Page) -> None:
                # A page can close for reasons that aren't a renderer crash
                # (browser/context tearing down, `window.close()`, a race
                # with tab management elsewhere) -- without this, `_Page.
                # alive` stayed `True` forever for those cases, so the next
                # action against it hit a raw `TargetClosedError` instead of
                # a typed, expected `ContextCrashed`.
                live_page = cctx.pages.get(pid)
                if live_page is not None and live_page.alive:
                    live_page.alive = False
                    live_page.death_reason = "page_closed"

            pg.on("crash", _on_crash)
            pg.on("close", _on_close)
            # Registering this listener is what turns Playwright's silent
            # auto-dismiss off, so it must happen for every page including
            # popups -- and `_page_session` must wrap the session to match.
            live_page = cctx.pages.get(pid)
            if live_page is not None:
                live_page.dialogs.set_policy(cctx.dialog_policy)
                live_page.dialogs.attach(pg)

        def _on_context_close(_context: BrowserContext) -> None:
            cctx.alive = False
            cctx.death_reason = cctx.death_reason or "context_closed"

        def _on_new_page(new_page: Page) -> None:
            if cctx.expecting_explicit_tab:
                # `_create_tab`'s own `context.new_page()` call -- register
                # plainly and let it take over from here (navigate, resolve
                # the page_id by identity); none of the popup-blocking/cap/
                # auto-focus policy below applies to a tab the caller
                # explicitly asked for via `NewTabAction`.
                new_page_id = str(uuid.uuid4())
                cctx.pages[new_page_id] = _Page(page=new_page)
                _wire_page(new_page_id, new_page)
                return
            if cctx.block_popups:
                asyncio.create_task(new_page.close())
                return
            if len(cctx.pages) >= cctx.max_tabs:
                asyncio.create_task(new_page.close())
                log.warning(
                    "driver.tab_cap_exceeded", context_id=context_id, max_tabs=cctx.max_tabs
                )
                return
            new_page_id = str(uuid.uuid4())
            cctx.pages[new_page_id] = _Page(page=new_page)
            # Auto-focus, matching real-browser semantics for a target=_blank
            # click -- same as a prior internal system's unconditional
            # new-window auto-follow (confirmed with user, see multi-tab plan).
            cctx.active_page_id = new_page_id
            cctx.page_changed = True
            _wire_page(new_page_id, new_page)

        _wire_page(page_id, page)
        context.on("close", _on_context_close)
        context.on("page", _on_new_page)

        self._contexts[context_id] = cctx
        return ContextRef(
            context_id=context_id,
            identity=identity,
            state=ContextState.ACTIVE,
            pid=None,  # resolved via cdp_patches on demand in P1, when interaction actions need it
            node_id=self._node_id,
        )

    async def close(self, ctx: ContextRef) -> None:
        cctx = self._contexts.pop(ctx.context_id, None)
        if cctx is None:
            return
        for live in cctx.pages.values():
            for session in live.frame_sessions.values():
                with contextlib.suppress(Exception):
                    await session.detach()
            live.frame_sessions.clear()
        if not cctx.alive:
            return
        if cctx.remote_browser is not None:
            # Detach, don't destroy. We attached to a browser someone else runs
            # and may still be using; closing its context would take their pages
            # with it. `browser.close()` on a connect_over_cdp browser
            # disconnects the websocket and leaves Chrome running.
            with contextlib.suppress(Exception):
                await cctx.remote_browser.close()
            return
        await cctx.context.close()

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        """Non-blocking process-exit check (agent-browser's pre-CDP probe,
        changelog #1023): `signal 0` tests existence without touching the
        process. Unknown pid -> not a death signal on its own (defer to the
        CDP ping); a permission error means it exists under another uid."""

        if pid is None:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    async def _cdp_ping(self, cctx: _Context) -> bool:
        """A cheap CDP round trip (`Page.getLayoutMetrics` -- Page domain, never
        Runtime, the same stealth constraint the fusion capture honors) proving
        Chrome is *responsive*, not merely present. Bounded so a wedged renderer
        reports 'not alive' in ~2s instead of hanging the caller. Reuses the
        page's live CDP session when one exists, else a short-lived one."""

        live = cctx.pages.get(cctx.active_page_id)
        if live is None or live.page.is_closed():
            return False
        cdp = live.cdp_session
        owns_session = cdp is None
        if cdp is None:
            try:
                cdp = await live.page.context.new_cdp_session(live.page)
            except Exception:
                return False
        try:
            await asyncio.wait_for(
                cdp.send("Page.getLayoutMetrics"), timeout=_LIVENESS_TIMEOUT_S
            )
            return True
        except Exception:
            return False
        finally:
            if owns_session:
                with contextlib.suppress(Exception):
                    await cdp.detach()

    async def is_alive(self, ctx: ContextRef) -> bool:
        cctx = self._contexts.get(ctx.context_id)
        if cctx is None or not cctx.alive:
            return False
        if not self._pid_alive(ctx.pid):
            return False
        return await self._cdp_ping(cctx)

    async def keepalive(self, ctx: ContextRef) -> bool:
        cctx = self._contexts.get(ctx.context_id)
        if cctx is None or not cctx.alive:
            return False
        return await self._cdp_ping(cctx)

    async def execute(
        self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
    ) -> ActionResult:
        """Dispatches the whole batch against one resolved page (`page_id`,
        defaulting to the context's active tab), aborting early if the page
        navigated out from under a ref-consuming action.

        `Action.terminates_sequence` is caller-facing metadata (e.g. an SDK
        deciding whether to keep queuing actions client-side); it is not the
        enforcement mechanism here. The actual guard is the runtime URL diff:
        a `NavigateAction` immediately followed by `SnapshotAction`/
        `ExtractAction` is the normal P0 pattern and must not abort, but once
        P1's ref-consuming actions (Click/Fill/...) land, any of them
        following an unnoticed navigation -- deliberate or not -- would act
        on stale refs, so those are the ones that trip `sequence_aborted`.

        Tab-management actions (New/Close/SwitchTab) mutate `cctx`'s
        bookkeeping for *subsequent* `execute()` calls; they do not retarget
        which page the rest of *this* batch dispatches against -- that stays
        fixed to `live`, resolved once here. See `spi.actions`' docstring on
        those actions for why."""

        cctx = self._require_context(ctx)
        live = self._require_page(cctx, page_id)
        result = ActionResult(page_changed=cctx.page_changed)
        cctx.page_changed = False
        cctx.health.tasks += 1
        metrics.incr("context_tasks_total")

        succeeded = False
        try:
            stale = False
            dispatched_any = False
            for action in actions:
                consumes_ref = isinstance(action, _REF_CONSUMING) or (
                    isinstance(action, (WaitAction, ScrollAction)) and action.ref is not None
                )
                if stale and consumes_ref:
                    result.sequence_aborted = True
                    break
                # Human between-actions dwell before an interactive action that
                # follows another -- paces a click/fill/press/scroll sequence the
                # way a person does, without slowing plain navigate+extract scrapes.
                if dispatched_any and isinstance(action, _GAP_BEFORE):
                    await cctx.delay_policy.pause("gap")
                try:
                    pre_url = live.page.url
                    await self._dispatch(cctx, live, action, result)
                    dispatched_any = True
                except DialogInterrupt as exc:
                    # Not a failure: the page asked a question and is waiting for
                    # an answer, which is exactly what a `"manual"` policy was
                    # chosen for. Report the dialog and stop -- every later
                    # action in the batch would block on the same renderer, and
                    # answering is a decision only the caller can make.
                    result.dialog = exc.info
                    result.sequence_aborted = True
                    result.verifications.append(
                        f"page is blocked on a {exc.info.describe()} -- "
                        "answer it with dialog_accept or dialog_dismiss"
                    )
                    break
                except TargetClosedError as exc:
                    # Belt-and-suspenders alongside `_wire_page`'s "close"
                    # listener above: that listener and the actual close can
                    # still race (this action's own CDP call landing in the gap
                    # between the page closing and the event handler running),
                    # so this is the last line of defense against a raw
                    # Playwright error leaking out as an opaque 500 instead of
                    # the typed `ContextCrashed` every other dead-page path uses.
                    live.alive = False
                    live.death_reason = live.death_reason or "page_closed"
                    raise ContextCrashed(str(exc)) from exc
                if live.page.url != pre_url:
                    stale = True
            succeeded = True
            return result
        finally:
            # Health is instrumentation only (Wave 0): a batch that raised
            # (ContextCrashed, etc.) is a failure; a batch that completed --
            # including one that set `sequence_aborted` -- is a driver-level
            # success. Nothing acts on these tallies yet.
            if succeeded:
                cctx.health.successes += 1
                metrics.incr("context_task_outcomes_total", outcome="success")
            else:
                cctx.health.failures += 1
                metrics.incr("context_task_outcomes_total", outcome="failure")

    async def _dispatch(
        self, cctx: _Context, live: _Page, action: Action, result: ActionResult
    ) -> None:
        # While a dialog is open the renderer is blocked, so *nothing* but
        # answering it can make progress -- not a click, not a `page.evaluate`,
        # not reading the title. Refusing up front turns what would otherwise be
        # a hang into the same `DialogInterrupt` the guard raises, and keeps the
        # rule in one place rather than at every call that happens to touch the
        # page.
        pending = live.dialogs.pending
        if pending is not None and not isinstance(action, _DIALOG_ACTIONS):
            raise DialogInterrupt(pending)

        if isinstance(action, NavigateAction):
            try:
                goto_kwargs: dict[str, Any] = {
                    "timeout": action.timeout_ms,
                    "wait_until": action.wait_until,
                }
                if action.referer is not None:
                    goto_kwargs["referer"] = action.referer
                response = await live.page.goto(action.url, **goto_kwargs)
            except PlaywrightTimeoutError as exc:
                raise NavigationTimeout(str(exc)) from exc
            # Any ref taken before this navigation must not resolve against
            # the new page -- drop the index the same way SnapshotAction does,
            # rather than relying on every caller to re-snapshot before reusing
            # a ref. Without this, a stale ref left in the index could resolve
            # against an unrelated element on the new page.
            live.epoch += 1
            live.nodes.reset()
            result.verifications.append(f"navigated to {live.page.url}")
            await self._post_navigate(cctx, live, result, response)
        elif isinstance(action, GoBackAction):
            try:
                await live.page.go_back(
                    wait_until=action.wait_until, timeout=action.timeout_ms
                )
            except PlaywrightTimeoutError as exc:
                raise NavigationTimeout(str(exc)) from exc
            live.epoch += 1
            live.nodes.reset()
            result.verifications.append(f"went back to {live.page.url}")
        elif isinstance(action, SnapshotAction):
            if action.settle:
                await self._settle(live)
            live.epoch += 1
            # CDP DOM/Snapshot/AX fusion -> EnhancedDOMTreeNode with stable
            # (session_id, backendNodeId) identity and cross-step change
            # detection. Refs are `e<selector_index>`, resolved to a captured
            # node by dictionary lookup and acted on over CDP.
            tree = await self._capture_fused(live, no_runtime=action.no_runtime)
            live.nodes.record(tree)
            live.last_tree = tree
            result.fused_trees.append(tree)
            # Index-correlated with `fused_trees`: the driver is the only place
            # that sees both the action's options and the tree, and serialization
            # happens in the caller.
            result.snapshot_views.append(await self._snapshot_view(live, action))
        elif isinstance(action, DiffSnapshotAction):
            if action.settle:
                await self._settle(live)
            previous = live.last_tree
            live.epoch += 1
            tree = await self._capture_fused(live, no_runtime=action.no_runtime)
            live.nodes.record(tree)
            live.last_tree = tree
            result.fused_trees.append(tree)
            result.snapshot_views.append(SnapshotView())
            if previous is None:
                result.readouts.append(
                    "no previous snapshot on this tab to compare against"
                )
            else:
                diff = diff_snapshots(previous, tree)
                result.readouts.append(
                    render_change_block(diff) or "nothing changed since the last snapshot"
                )
        elif isinstance(action, ExtractAction):
            # Lazy, once-per-batch: cheap dedicated CDP getter, not tied to a
            # full page.content() fetch -- doesn't add a round trip to
            # non-extract batches (interactive click/fill/etc. sequences),
            # and isn't refetched if a batch requests multiple formats.
            if result.page_title is None:
                result.page_title = await live.dialogs.guard(live.page.title())
            html = await live.dialogs.guard(live.page.content())
            base_url = action.base_url or live.page.url
            live_hydration: dict[str, Any] | None = None
            if action.format == "structured_data":
                # Static parse first (cheap, in-memory HTML reparse); only
                # fall back to a live JS-eval round trip if it found nothing
                # -- covers client-only SPA state never present in the
                # static markup, without paying for it on every scrape.
                preview = json.loads(extract(html, format=action.format, base_url=base_url))
                if not preview["hydration"]:
                    live_hydration = await self._probe_hydration_globals(live)
            result.extracts.append(
                extract(
                    html,
                    format=action.format,
                    main_content=action.main_content,
                    include_tags=action.include_tags,
                    exclude_tags=action.exclude_tags,
                    base_url=base_url,
                    live_hydration=live_hydration,
                )
            )
        elif isinstance(action, ScreenshotAction):
            result.screenshots.append(
                await live.dialogs.guard(live.page.screenshot(full_page=action.full_page))
            )
        elif isinstance(action, WaitAction):
            if action.ref is not None:
                node, cdp = await self._resolve_ref(live, action.ref)
                await cdp_element.element_box(cdp, node, action.ref)
            else:
                await asyncio.sleep((action.ms or 0) / 1000)
        elif isinstance(action, ExecuteJsAction):
            result.js_returns.append(await live.dialogs.guard(live.page.evaluate(action.script)))
        elif isinstance(action, ClickAction):
            pre_click_url = live.page.url
            node, cdp = await self._resolve_ref(live, action.ref)
            await self._human_click(cdp, node, action.ref, cctx.delay_policy)
            if live.page.url != pre_click_url:
                # A click that navigated returns as soon as the event is
                # dispatched, so the new document may not exist yet -- the very
                # next action (an extract, a snapshot) would then read the *old*
                # page and look like it simply saw no change. Bounded and
                # best-effort: a page that never settles still returns whatever
                # it has, which is better than failing an action that worked.
                await self._await_document(live)
                result.verifications.append(f"click on {action.ref} navigated to {live.page.url}")
            else:
                result.verifications.append(f"clicked {action.ref}")
        elif isinstance(action, FillAction):
            node, cdp = await self._resolve_ref(live, action.ref)
            await self._human_fill(
                cdp, node, action.ref, action.text, cctx.delay_policy, clear=action.clear
            )
            # Read-back grounding: confirm the value actually landed in the
            # field rather than assuming success. Best-effort -- a field that
            # can't report its value must not fail the fill.
            value = await cdp_element.read_value(cdp, node)
            if value is not None:
                result.verifications.append(
                    f"filled {action.ref}: field now contains {value[:80]!r}"
                )
        elif isinstance(action, SelectOptionAction):
            await self._select_option(live, action, result)
        elif isinstance(action, HoverAction):
            node, cdp = await self._resolve_ref(live, action.ref)
            box = await cdp_element.element_box(cdp, node, action.ref)
            await self._approach(cdp, box)
        elif isinstance(action, PressAction):
            await cdp_element.press_key(await self._page_session(live), action.key)
            await cctx.delay_policy.pause("press")
        elif isinstance(action, ScrollAction):
            await self._scroll(live, action)
        elif isinstance(action, SendKeysAction):
            await cdp_element.press_key(await self._page_session(live), action.keys)
            await cctx.delay_policy.pause("press")
        elif isinstance(action, FindTextAction):
            found = await self._find_text(live, action.text)
            result.verifications.append(
                f"scrolled to {action.text!r}"
                if found
                else f"text {action.text!r} was not found on this page"
            )
        elif isinstance(action, DropdownOptionsAction):
            node, _ = await self._resolve_ref(live, action.ref)
            options = cdp_element.dropdown_options(node)
            result.readouts.append(_render_dropdown_options(action.ref, options))
        elif isinstance(action, SearchPageAction):
            result.readouts.append(await self._search_page(live, action))
        elif isinstance(action, FindElementsAction):
            result.readouts.append(await self._find_elements(live, action))
        elif isinstance(action, UploadFileAction):
            node, cdp = await self._resolve_ref(live, action.ref)
            await cdp_element.set_file_input(cdp, node, [action.path])
            result.verifications.append(f"attached {action.path} to {action.ref}")
        elif isinstance(action, DialogStatusAction):
            pending = live.dialogs.pending
            result.dialog = pending
            result.readouts.append(
                pending.describe()
                if pending is not None
                else "no JavaScript dialog is open on this page"
            )
        elif isinstance(action, DialogAcceptAction):
            answered = await live.dialogs.accept(action.prompt_text)
            await self._after_dialog(live, result, answered, "accepted")
        elif isinstance(action, DialogDismissAction):
            answered = await live.dialogs.dismiss()
            await self._after_dialog(live, result, answered, "dismissed")
        elif isinstance(action, NewTabAction):
            new_page_id = await self._create_tab(cctx, action.url)
            cctx.active_page_id = new_page_id
        elif isinstance(action, CloseTabAction):
            await self._close_tab(cctx, action.page_id)
        elif isinstance(action, SwitchTabAction):
            if action.page_id not in cctx.pages:
                raise TabNotFound(action.page_id)
            cctx.active_page_id = action.page_id
        elif isinstance(action, ListTabsAction):
            result.tabs.append(await self._list_tabs(cctx))
        else:
            assert_never(action)

    async def _snapshot_view(self, live: _Page, action: SnapshotAction) -> SnapshotView:
        """Turn a `SnapshotAction`'s options into the filters the serializer
        applies, resolving the two that need a live page.

        Everything else on `SnapshotView` is already a plain value; only
        `selector` (a CSS query) and `viewport_only` (the live viewport
        rectangle) have to be asked of the browser, which is why this lives here
        and `dom.serializer` stays pure.
        """

        viewport: BoundingBox | None = None
        if action.viewport_only:
            cdp = await self._page_session(live)
            width, height = await cdp_element.viewport_size(cdp)
            # Page coordinates, not client coordinates: `absolute_position` on a
            # fused node is document-relative, so the viewport has to be offset
            # by the current scroll or everything below the fold looks in-view
            # after the first scroll.
            offset = await live.dialogs.guard(
                live.page.evaluate("() => [window.scrollX, window.scrollY]")
            )
            viewport = BoundingBox(float(offset[0]), float(offset[1]), width, height)

        scope: frozenset[int] | None = None
        if action.selector is not None:
            scope = await self._selector_scope(live, action.selector)

        return SnapshotView(
            scope=scope,
            roles=action.roles,
            viewport=viewport,
            max_nodes=action.max_nodes,
            depth=action.depth,
        )

    async def _selector_scope(self, live: _Page, selector: str) -> frozenset[int]:
        """Backend node ids of the element matching `selector` and everything
        under it.

        `DOM.querySelector` needs a document node id, so the document is fetched
        first. A selector that matches nothing raises rather than silently
        scoping the observation to the empty set -- a snapshot that came back
        blank because of a typo should say so, not look like an empty page.
        """

        cdp = await self._page_session(live)
        document = await cdp.send("DOM.getDocument", {"depth": 0})
        root_id = document["root"]["nodeId"]
        match = await cdp.send(
            "DOM.querySelector", {"nodeId": root_id, "selector": selector}
        )
        node_id = match.get("nodeId")
        if not node_id:
            raise SelectorNotFound(selector)

        described = await cdp.send("DOM.describeNode", {"nodeId": node_id, "depth": -1})
        ids: set[int] = set()
        _collect_backend_ids(described.get("node") or {}, ids)
        return frozenset(ids)

    async def _after_dialog(
        self, live: _Page, result: ActionResult, answered: DialogInfo, verb: str
    ) -> None:
        """Finish answering a dialog: replay a held mouse button, then let the
        page settle.

        The release matters. A `confirm()` opened from a `mousedown` handler
        leaves the button logically down -- the CDP `mouseReleased` that would
        have followed was the command the dialog blocked. Left held, the next
        click arrives as a drag, or as the second half of a double-click.
        agent-browser replays it for the same reason
        (`interaction.rs::dispatch_pending_release`).
        """

        release = live.dialogs.pending_release
        live.dialogs.pending_release = None
        if release is not None:
            with contextlib.suppress(Exception):
                await cdp_element.dispatch_mouse(
                    release.session, "mouseReleased", release.x, release.y, button="left"
                )

        # Answering is what unblocks whatever the dialog gated -- commonly a
        # navigation. Give it the same bounded settle a click that navigated
        # gets, or the next action reads the page the dialog was holding.
        await self._await_document(live)
        result.verifications.append(f"{verb} the {answered.describe()}; now at {live.page.url}")

    async def _create_tab(self, cctx: _Context, url: str | None) -> str:
        """Explicit `NewTabAction` counterpart to `open()`'s `_on_new_page`
        auto-tracking -- same cap enforcement, no popup-blocking check (a
        caller-requested tab isn't a popup). Registration and crash-handler
        wiring happen inside `_on_new_page` itself, not here: `context.
        new_page()` fires that same context-level `"page"` event an organic
        popup does, so `expecting_explicit_tab` routes it through the one
        real registration path instead of this method minting a second,
        duplicate `_Page` for the same underlying Playwright `Page`."""

        if len(cctx.pages) >= cctx.max_tabs:
            raise CapacityExhausted(f"session already has {cctx.max_tabs} tabs open")
        cctx.expecting_explicit_tab = True
        try:
            new_page = await cctx.context.new_page()
        finally:
            cctx.expecting_explicit_tab = False
        page_id = next(pid for pid, p in cctx.pages.items() if p.page is new_page)
        if url is not None:
            await new_page.goto(url)
        return page_id

    async def _close_tab(self, cctx: _Context, page_id: str) -> None:
        live = cctx.pages.get(page_id)
        if live is None:
            raise TabNotFound(page_id)
        if len(cctx.pages) == 1:
            raise ValueError("cannot close the last remaining tab in a session")
        if live.cdp_session is not None:
            with contextlib.suppress(Exception):
                await live.cdp_session.send("Page.stopScreencast")
                await live.cdp_session.detach()
        # Frame sessions outlive a single snapshot now, so closing the tab is
        # what releases them.
        for session in live.frame_sessions.values():
            with contextlib.suppress(Exception):
                await session.detach()
        live.frame_sessions.clear()
        with contextlib.suppress(Exception):
            await live.page.close()
        del cctx.pages[page_id]
        if cctx.active_page_id == page_id:
            cctx.active_page_id = next(iter(cctx.pages))
            cctx.page_changed = True

    async def _list_tabs(self, cctx: _Context) -> list[TabInfo]:
        titles = await asyncio.gather(
            *(p.page.title() for p in cctx.pages.values()), return_exceptions=True
        )
        return [
            TabInfo(
                page_id=pid,
                url=p.page.url,
                title=title if isinstance(title, str) else "",
                active=(pid == cctx.active_page_id),
            )
            for (pid, p), title in zip(cctx.pages.items(), titles, strict=True)
        ]

    _HYDRATION_PROBE_JS = """() => {
        const keys = ["__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__",
                      "__APOLLO_STATE__", "__REDUX_STATE__"];
        const found = {};
        for (const key of keys) {
            if (window[key] !== undefined) {
                found[key] = window[key];
            }
        }
        return found;
    }"""

    async def _probe_hydration_globals(self, live: _Page) -> dict[str, Any]:
        """Live JS-eval fallback for `structured_data` extraction, used only
        when the static HTML parse found no hydration script tag -- covers
        client-only SPA state that never lands in the served markup. Best
        effort: a window global holding something CDP can't JSON-serialize
        (a function, a circular ref) fails closed to `{}` rather than
        breaking the whole extract."""

        try:
            result = await live.page.evaluate(self._HYDRATION_PROBE_JS)
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    async def _install_resource_blocking(
        self, context: BrowserContext, resource_types: tuple[str, ...], hosts: tuple[str, ...]
    ) -> None:
        """Abort requests whose Playwright `resource_type` is in `resource_types`
        or whose URL contains any of `hosts` -- the idiomatic Patchright
        equivalent of Pulsar's `Network.setBlockedURLs` (`PulsarWebDriver.kt:643`).
        Registered on the context so it covers every page. A route error must
        never fail the request, so anything unexpected falls through to
        `continue_()`."""

        type_set = frozenset(resource_types)

        async def _route(route: Any) -> None:
            try:
                req = route.request
                if req.resource_type in type_set or any(h in req.url for h in hosts):
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                with contextlib.suppress(Exception):
                    await route.continue_()

        await context.route("**/*", _route)

    async def _post_navigate(
        self, cctx: _Context, live: _Page, result: ActionResult, response: Any
    ) -> None:
        """Warm-up + block detection after a navigation -- both opt-in per
        `open()` (`cctx.warmup` / `cctx.detect_blocks`), so the interactive/test
        callers that opt out get exactly the prior status-only behaviour.

        When `detect_blocks` is on we fetch the page body once and classify it
        (`block_detect.classify_page`): a bot wall raises `ChallengeDetected`
        (the session layer's cue to tear down + rotate), while softer verdicts
        just accrue weighted `leak_warnings` for burn accounting. This is what
        finally catches Akamai's HTTP-200 Access Denied, which the status-only
        check silently returned as content."""

        status = response.status if response is not None else None
        result.status_code = status

        if cctx.warmup:
            # STEALTH timing: warm-up only runs for protected targets. Never
            # let the flourish fail the navigation it follows.
            with contextlib.suppress(Exception):
                await warmup.warm_up(live.page, humanize.STEALTH, wait_abck=cctx.detect_blocks)

        if cctx.detect_blocks:
            html: str | None = None
            with contextlib.suppress(Exception):
                html = await live.page.content()
            # `live.page.url` is the browser's *final* location, so a silent
            # 200-redirect to a block page is already visible to the site
            # checkers (what Pulsar reads `activeDOMUrls.location` for).
            # Headers additionally catch a DataDome/PerimeterX challenge that
            # is visually indistinguishable from a thin real page.
            headers = response.headers if response is not None else None
            verdict = block_detect.classify_page(
                html=html,
                url=live.page.url,
                status=status,
                headers=headers,
                hooks=self._block_hooks,
            )
            weight = block_detect.warning_weight(verdict)
            scope = block_detect.retry_scope(verdict)
            if weight:
                cctx.health.leak_warnings += weight
                metrics.incr("context_leak_warnings_total", reason=verdict.value)
            if scope is block_detect.Scope.PRIVACY:
                # A hard wall: rotate the whole identity (fresh proxy+fingerprint).
                raise ChallengeDetected(
                    f"{verdict.value} at {live.page.url}",
                    verdict=verdict.value,
                    weight=weight,
                    scope="privacy",
                )
            if scope is block_detect.Scope.CRAWL:
                # A soft failure: the page rendered, so return its content, but
                # flag it so the session layer can choose a cheap same-identity
                # retry (Pulsar's CRAWL retry scope) rather than rotate.
                result.soft_verdict = verdict.value
                result.soft_weight = weight
                result.verifications.append(
                    f"warning: page classified {verdict.value} (soft, retryable)"
                )
            elif verdict is not block_detect.Verdict.OK:
                result.verifications.append(f"warning: page classified {verdict.value}")
            return

        # Status-only fallback (unchanged) when detect_blocks is off.
        reason = _navigation_leak_reason(status)
        if reason is not None:
            cctx.health.leak_warnings += 1
            metrics.incr("context_leak_warnings_total", reason=reason)
            result.verifications.append(
                f"warning: navigation returned {reason} -- possible block or bot challenge"
            )

    async def _await_document(self, live: _Page) -> None:
        """Wait, briefly, for a click-triggered navigation's document to parse.

        `domcontentloaded` rather than `load`, for the same reason
        `NavigateAction` defaults to it: waiting on every subresource means a
        page with one slow third-party frame never returns. Swallows everything
        -- this is making a subsequent read see the right page, not a guarantee
        the caller can rely on, and a click that worked must not fail here."""

        with contextlib.suppress(Exception):
            await live.page.wait_for_load_state(
                "domcontentloaded", timeout=_CLICK_NAVIGATION_TIMEOUT_MS
            )

    async def _settle(self, live: _Page) -> None:
        """Best-effort, bounded wait for the page to reach network-idle before
        an opt-in (`SnapshotAction.settle`) snapshot, so the agent perceives a
        stable page across steps. Swallows everything -- including the expected
        timeout on pages that never go idle -- because a failed settle must
        never fail the snapshot it precedes."""

        with contextlib.suppress(Exception):
            await live.page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT_MS)

    async def _resolve_ref(
        self, live: _Page, ref: str
    ) -> tuple[EnhancedDOMTreeNode, CDPSession]:
        """A ref to the captured node plus the CDP session that owns it.

        A dictionary lookup and nothing more -- this is browser-use's model
        (`browser/session.py:2451-2466`, "pure in-memory dict lookup, zero CDP").
        The previous implementation tried to *re-derive* a CSS/XPath selector for
        an element it had already captured and required the selector to match
        exactly one live element, which duplicate ids and repeated
        `name`/`aria-label` routinely broke, and which could not reach into an
        iframe at all.

        No visibility gate here either. The old one called `bounding_box()` with
        a 3s cap and treated a zero-size box as a stale ref, which quietly
        rejected elements that are legitimately actionable while having no box of
        their own -- a hidden `input[type=file]`, a checkbox behind a styled
        label. Geometry is now the concern of whichever verb actually needs a
        point to aim at, and `element_box` raises there if it truly has none.
        """

        node = live.nodes.get(ref)
        if node is None:
            raise StaleRefError(ref, epoch_superseded=False)
        cdp = cdp_element.session_for_node(
            node,
            page_session=await self._page_session(live),
            frame_sessions=live.frame_sessions,
        )
        return node, cdp

    async def _approach(self, cdp: CDPSession, box: mouse.Box) -> tuple[float, float]:
        """Move the pointer to a jittered point inside `box` along an
        interpolated path, and return the point landed on.

        The approach matters more than the destination: a pointer that
        materialises inside an element never emits the mouseover/mouseenter
        transition a real one does, and keystroke/pointer telemetry reads a
        single teleport to an element's exact geometric centre as synthetic.
        Best effort -- if any move fails the caller still has a valid target.
        """

        width, height = await cdp_element.viewport_size(cdp)
        target = cdp_element.clamp(mouse.jittered_point_in(box), width, height)
        with contextlib.suppress(Exception):
            start = cdp_element.clamp(mouse.approach_from_outside(box), width, height)
            await cdp_element.move_to(cdp, *start)
            for x, y in mouse.path(start, target):
                await cdp_element.move_to(cdp, x, y)
                await asyncio.sleep(random.uniform(0.006, 0.018))
        return target

    async def _human_click(
        self,
        cdp: CDPSession,
        node: EnhancedDOMTreeNode,
        ref: str,
        policy: humanize.DelayPolicy,
    ) -> None:
        """Scroll into view, approach, dwell, click -- all as CDP input events on
        the node's own session.

        Dispatching on that session rather than through `page.mouse` is what lets
        a click land inside a cross-origin iframe: the frame is a separate
        renderer, its geometry is reported in its own coordinate space, and a
        page-level mouse event never reaches it.
        """

        box = await cdp_element.element_box(cdp, node, ref)
        await self._approach(cdp, box)
        await policy.pause("click")

        # Re-measure immediately before pressing. The approach above is
        # deliberately unhurried -- interpolated moves with jittered sleeps, then
        # a dwell -- which puts a few hundred milliseconds between measuring the
        # element and clicking it. That is ample time for a lazy image, a late
        # font or a settling layout to move the target, and the click then lands
        # on whatever slid into its place: an action that reports success and
        # does something else, or nothing.
        target = await self._aim(cdp, node, ref, box)
        await cdp_element.click_at(cdp, *target)

    async def _aim(
        self,
        cdp: CDPSession,
        node: EnhancedDOMTreeNode,
        ref: str,
        fallback: mouse.Box,
    ) -> tuple[float, float]:
        """The point to click, measured now. Falls back to the earlier box if the
        element has stopped reporting geometry -- better a click at the last
        known position than an action that fails outright."""

        try:
            box = await cdp_element.element_box(cdp, node, ref)
        except Exception:
            box = fallback
        width, height = await cdp_element.viewport_size(cdp)
        return cdp_element.clamp(mouse.jittered_point_in(box), width, height)

    async def _human_fill(
        self,
        cdp: CDPSession,
        node: EnhancedDOMTreeNode,
        ref: str,
        text: str,
        policy: humanize.DelayPolicy,
        *,
        clear: bool = True,
    ) -> None:
        """Focus, optionally clear, then type character-by-character with a
        randomized inter-key delay from the tier's `type` preset.

        Not a single value write: an instantaneous DOM set is a strong automation
        tell to keystroke-timing telemetry, and it skips the per-keystroke
        handlers autocomplete and validation widgets hang off. The delay scales
        with the tier (STEALTH types slower than FAST).

        Focus is `DOM.focus` with a click fallback, which is how a person focuses
        a field and is what works for the custom widgets whose real focus target
        is a child element.
        """

        if not await cdp_element.focus(cdp, node):
            box = await cdp_element.element_box(cdp, node, ref)
            target = await self._approach(cdp, box)
            await cdp_element.click_at(cdp, *target)

        if clear:
            await self._clear_field(cdp, node, ref)
        else:
            # Appending: put the caret at the end, or the typed text lands
            # wherever the click left it -- usually mid-value.
            await cdp_element.press_key(cdp, "End")
        for ch in text:
            await cdp_element.type_character(cdp, ch)
            await policy.pause("type")

    async def _scroll(self, live: _Page, action: ScrollAction) -> None:
        """Scroll the page, or an element's own scroll container.

        With a `ref`, the wheel event is dispatched at the element's centre in
        its own session rather than scrolling the element into view: a dropdown
        list, a virtualised table or an iframe scrolls its *own* overflow, which
        `scrollIntoView` on the container cannot do and which is what an agent
        working through a long list actually needs.
        """

        if action.ref is None:
            cdp = await self._page_session(live)
            width, height = await cdp_element.viewport_size(cdp)
            dx, dy = _scroll_delta(action.direction, action.pages, width, height)
            await cdp_element.scroll_gesture(cdp, width / 2, height / 2, dx, dy)
            return

        node, cdp = await self._resolve_ref(live, action.ref)
        box = await cdp_element.element_box(cdp, node, action.ref)
        # An element's own scroll distance is measured in *its* height, not the
        # viewport's: one "page" of a short dropdown list is a short scroll.
        dx, dy = _scroll_delta(action.direction, action.pages, box["width"], box["height"])
        # A wheel event, not the gesture the page path uses: the gesture drives
        # the *root* scroller, so aiming it at a dropdown list scrolls the page
        # underneath and leaves the list untouched.
        await cdp_element.wheel_at(
            cdp, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, dx, dy
        )

    async def _find_text(self, live: _Page, text: str) -> bool:
        """Scroll to the first node containing `text`, via Chrome's own
        find-in-page.

        `DOM.performSearch` takes a plain string and handles the escaping and the
        text-node walking itself, which is why this does not build an XPath --
        browser-use interpolates the model's text straight into
        `//*[contains(text(), "...")]`, where a quote in the search term breaks
        the expression.
        """

        cdp = await self._page_session(live)
        try:
            search = await cdp.send("DOM.performSearch", {"query": text})
            search_id, count = search.get("searchId"), search.get("resultCount", 0)
            if not search_id or not count:
                return False
            try:
                found = await cdp.send(
                    "DOM.getSearchResults",
                    {"searchId": search_id, "fromIndex": 0, "toIndex": min(count, 1)},
                )
                node_ids = found.get("nodeIds") or []
                if not node_ids:
                    return False
                await cdp.send("DOM.scrollIntoViewIfNeeded", {"nodeId": node_ids[0]})
                return True
            finally:
                with contextlib.suppress(Exception):
                    await cdp.send("DOM.discardSearchResults", {"searchId": search_id})
        except Exception:
            return False

    async def _search_page(self, live: _Page, action: SearchPageAction) -> str:
        """Grep the page's rendered text.

        Runs in the page because the text has to come from the *rendered* DOM,
        which the fused tree deliberately prunes and truncates. Parameters are
        passed as arguments rather than interpolated into the source, so a regex
        containing a quote or a backslash cannot break out of the script.
        """

        try:
            matches = await live.page.evaluate(
                _SEARCH_PAGE_JS,
                {
                    "pattern": action.pattern,
                    "regex": action.regex,
                    "caseSensitive": action.case_sensitive,
                    "contextChars": action.context_chars,
                    "maxResults": action.max_results,
                    "cssScope": action.css_scope,
                },
            )
        except Exception as exc:
            return f"search_page for {action.pattern!r} failed: {exc}"

        found = matches.get("matches") or []
        if not found:
            return f"search_page: no match for {action.pattern!r} on this page"
        total = matches.get("total", len(found))
        lines = [f"search_page: {total} match(es) for {action.pattern!r}"]
        lines += [f"[{i}] …{m}…" for i, m in enumerate(found, start=1)]
        return "\n".join(lines)

    async def _find_elements(self, live: _Page, action: FindElementsAction) -> str:
        """Query the DOM by CSS selector and read back what matched."""

        try:
            found = await live.page.evaluate(
                _FIND_ELEMENTS_JS,
                {
                    "selector": action.selector,
                    "attributes": list(action.attributes),
                    "maxResults": action.max_results,
                    "includeText": action.include_text,
                },
            )
        except Exception as exc:
            return f"find_elements for {action.selector!r} failed: {exc}"

        elements = found.get("elements") or []
        if not elements:
            return f"find_elements: nothing matched {action.selector!r}"
        lines = [
            f"find_elements: {found.get('total', len(elements))} match(es) "
            f"for {action.selector!r} (showing {len(elements)})"
        ]
        for i, element in enumerate(elements, start=1):
            parts = [f"<{element.get('tag')}>"]
            if element.get("text"):
                parts.append(repr(element["text"]))
            for key, value in (element.get("attrs") or {}).items():
                parts.append(f"{key}={value!r}")
            lines.append(f"[{i}] " + " ".join(parts))
        return "\n".join(lines)

    async def _select_option(
        self, live: _Page, action: SelectOptionAction, result: ActionResult
    ) -> None:
        """Choose options in a `<select>`.

        The one verb that cannot be driven by input events: a native select popup
        is browser chrome, not page content, so there is no coordinate to click.
        browser-use resolves the node and calls into it with
        `Runtime.callFunctionOn` (`default_action_watchdog.py:3288-3746`); this
        does the same, and is the only place in the interaction path that needs
        the `Runtime` domain.

        Matching accepts an option's visible text as well as its `value` --
        a model reads the label, not the markup. The available options come off
        the captured tree, so a value that matches nothing is reported *with the
        real choices*, which is a correctable answer rather than a bare failure.
        """

        node, cdp = await self._resolve_ref(live, action.ref)
        options = cdp_element.dropdown_options(node)
        if options:
            known = {opt["value"] for opt in options} | {opt["text"] for opt in options}
            unknown = set(action.values) - known
            if unknown:
                available = ", ".join(repr(opt["text"]) for opt in options[:20])
                result.verifications.append(
                    f"select_option on {action.ref}: no option matching {sorted(unknown)!r}. "
                    f"Available: {available}"
                )
                return

        outcome = await cdp_element.select_options(cdp, node, list(action.values))
        error = outcome.get("error")
        if error:
            result.verifications.append(f"select_option on {action.ref} failed: {error}")
        else:
            result.verifications.append(
                f"select_option on {action.ref}: selected {outcome.get('selected')}"
            )

    async def _clear_field(self, cdp: CDPSession, node: EnhancedDOMTreeNode, ref: str) -> None:
        """Empty a focused field by selecting its contents and deleting them.

        Triple-click then Delete, rather than a JS value assignment: it needs no
        `Runtime` call, so it works under the stealth tier, and it produces the
        same input/change events a person clearing a field would. Best effort --
        a field with no box to triple-click (a custom widget) still gets the
        select-all shortcut.
        """

        with contextlib.suppress(Exception):
            box = await cdp_element.element_box(cdp, node, ref)
            width, height = await cdp_element.viewport_size(cdp)
            point = cdp_element.clamp(mouse.jittered_point_in(box), width, height)
            await cdp_element.click_at(cdp, *point, click_count=3)
        await cdp_element.press_key(cdp, "ControlOrMeta+a")
        await cdp_element.press_key(cdp, "Delete")

    async def _attach_remote(
        self, playwright: Any, context_kwargs: dict[str, Any]
    ) -> tuple[BrowserContext, Any]:
        """Attach to a browser someone else is running, over CDP.

        This is the deployment shape where the browser lives in its own
        container: the client library and Chrome scale, crash and restart
        independently, and one browser can be shared. Everything downstream --
        pages, the node index, `cdp_element` -- is unchanged, because they only
        ever touch Playwright objects and those are the same either way.

        **What does not carry over, and is deliberately not faked.** A remote
        browser was launched by someone else, so the profile directory, the
        pinned fingerprint's launch arguments and the proxy are already decided
        and cannot be applied from here. Only the context-level settings that
        Playwright can still set on an existing browser (locale, timezone, user
        agent) are passed. A caller asking for a stealth tier over `cdp_url` is
        getting a weaker guarantee than the same tier locally, so it is logged
        rather than left to be discovered from behaviour.
        """

        url = await _resolve_cdp_url(self._launch.cdp_url or "")
        browser = await playwright.chromium.connect_over_cdp(url)

        # An already-running browser normally has a context; reuse it so we
        # inherit its cookies and storage rather than starting beside them in a
        # fresh incognito context the user cannot see.
        if browser.contexts:
            context = browser.contexts[0]
            if context_kwargs:
                log.info(
                    "driver.remote_context_settings_skipped",
                    cdp_url=url,
                    skipped=sorted(context_kwargs),
                )
        else:
            context = await browser.new_context(**context_kwargs)
        return context, browser

    async def _page_session(self, live: _Page) -> CDPSession:
        """This tab's CDP session, created once and kept.

        Previously a session was created and detached around every single
        snapshot. Beyond the churn on the agent's hot path, a per-call session
        cannot work at all now: the same session has to capture the DOM *and*
        dispatch the input events that act on it, because a `backendNodeId` is
        only meaningful to the session that issued it.

        Reuses the live-view screencast session when there is one, so a tab being
        watched does not carry two.
        """

        if live.cdp_session is None:
            session = await live.page.context.new_cdp_session(live.page)
            # Wrapped once, here, so every `cdp_element` call made through it is
            # guarded without that module knowing dialogs exist. See
            # `driver.dialogs.GuardedSession`.
            live.cdp_session = cast(CDPSession, GuardedSession(session, live.dialogs))
        return live.cdp_session

    async def _refresh_frame_sessions(self, live: _Page) -> dict[str, CDPSession]:
        """CDP frame id -> session, for every frame of this tab.

        Rebuilt per snapshot because frames come and go, but sessions are cached
        across snapshots (`live.frame_sessions`): attaching is the expensive part,
        and a frame that is still present is still addressable through the session
        that captured it.

        `context.new_cdp_session` accepts a `Frame`, which is how a cross-origin
        frame's own target is reached without hand-rolling
        `Target.setAutoAttach`. The frame's CDP id is read back from its own
        `Page.getFrameTree` -- Playwright does not expose it -- which is also what
        keys the node identity the driver later routes on.
        """

        if not self._cross_origin_iframes:
            return {}

        live_frames = {}
        for frame in live.page.frames:
            if frame is live.page.main_frame:
                continue
            try:
                session = await live.page.context.new_cdp_session(frame)
                tree = await session.send("Page.getFrameTree")
                frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
            except Exception:
                # A frame that navigated or detached mid-enumeration. Skipping it
                # costs visibility into that one frame, not the whole snapshot.
                continue
            if frame_id is not None:
                # Guarded like the page's own session: a `confirm()` raised from
                # inside an iframe blocks that frame's renderer, and the input
                # events this session carries are the ones that would hang on it.
                live_frames[frame_id] = cast(CDPSession, GuardedSession(session, live.dialogs))

        for frame_id, session in live.frame_sessions.items():
            if frame_id not in live_frames:
                with contextlib.suppress(Exception):
                    await session.detach()
        live.frame_sessions = live_frames
        return live_frames

    async def _capture_fused(self, live: _Page, *, no_runtime: bool):
        """Capture a fused `EnhancedDOMTreeNode` for the whole tab, cross-origin
        iframe content included. `no_runtime` (from the UI stealth tier) forbids
        the engine's only Runtime call."""

        cdp = await self._page_session(live)
        frame_sessions = await self._refresh_frame_sessions(live)
        return await capture_fused_tree(
            cdp, no_runtime=no_runtime, frame_sessions=frame_sessions
        )

    async def export_state(self, ctx: ContextRef) -> StorageState:
        cctx = self._require_context(ctx)
        raw = await cctx.context.storage_state()
        return _from_playwright_storage_state(raw)

    async def restore_state(self, ctx: ContextRef, state: StorageState) -> None:
        cctx = self._require_context(ctx)
        await cctx.context.set_storage_state(_to_playwright_storage_state(state))

    async def health(self, ctx: ContextRef) -> HealthStatus:
        """A context can now outlive any *one* of its tabs crashing (that's
        the whole point of multi-tab), so `cctx.alive` alone -- only ever
        flipped by the whole browser context closing -- isn't sufficient
        anymore: a context every one of whose tabs has crashed is just as
        dead as one that closed outright, even though nothing set
        `cctx.alive = False` for that case. Still healthy as long as at
        least one tracked tab survives, matching real multi-tab semantics."""

        cctx = self._contexts.get(ctx.context_id)
        if cctx is None:
            return HealthStatus(alive=False, reason="context_not_found")
        if not cctx.alive:
            return HealthStatus(alive=False, reason=cctx.death_reason)
        if any(p.alive for p in cctx.pages.values()):
            return HealthStatus(alive=True, reason=None)
        death_reason = next((p.death_reason for p in cctx.pages.values() if p.death_reason), None)
        return HealthStatus(alive=False, reason=death_reason)

    async def context_health(self, ctx: ContextRef) -> ContextHealth | None:
        cctx = self._contexts.get(ctx.context_id)
        if cctx is None:
            return None
        h = cctx.health
        return ContextHealth(
            tasks=h.tasks,
            successes=h.successes,
            failures=h.failures,
            small_pages=h.small_pages,
            leak_warnings=h.leak_warnings,
        )

    # --- LiveViewCapable (optional capability; see spi.streaming) ---
    # Page and Input domains only -- never Runtime, to preserve Patchright's
    # anti-leak guarantee (see agentpilot/driver/live_view.py's module docstring).
    # One screencast at a time per context (the active tab's), matching a
    # real browser's single visible tab -- see multi-tab plan's "explicitly
    # out of scope".

    async def start_screencast(
        self, ctx: ContextRef, page_id: str | None = None
    ) -> asyncio.Queue[LiveViewFrame]:
        cctx = self._require_context(ctx)
        live = self._require_page(cctx, page_id)
        if live.frame_queue is not None:
            live.frame_queue_refs += 1
            return live.frame_queue

        queue: asyncio.Queue[LiveViewFrame] = asyncio.Queue(maxsize=2)
        cdp = await cctx.context.new_cdp_session(live.page)

        async def _ack_and_enqueue(params: dict[str, Any]) -> None:
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            if queue.full():
                queue.get_nowait()  # live view wants the latest frame, not a backlog
            queue.put_nowait(parse_screencast_frame(params))

        cdp.on("Page.screencastFrame", lambda params: asyncio.create_task(_ack_and_enqueue(params)))
        await cdp.send("Page.startScreencast", SCREENCAST_START_PARAMS)

        # Only set once the screencast is confirmed started -- if `new_cdp_session`
        # or `startScreencast` raised above, `frame_queue`/`frame_queue_refs`
        # are left untouched rather than half-initialized.
        live.cdp_session = cdp
        live.frame_queue = queue
        live.frame_queue_refs = 1
        return queue

    async def stop_screencast(self, ctx: ContextRef, page_id: str | None = None) -> None:
        """Reference-counted, not unconditional: `routes/live_view.py` calls
        this from every websocket's own `finally`, but `useLiveView`'s
        reconnect-on-`page_id`-resolution dance (mount with `page_id=None`,
        then immediately reconnect once the real id is known) means a second
        connection routinely calls `start_screencast` again -- and reuses
        this same queue/cdp_session, per the check above -- before the first
        connection's teardown runs. Tearing down unconditionally here used to
        detach the *shared* `cdp_session` and null out `frame_queue` out from
        under that second, still-open connection, orphaning its `_send_frames`
        loop on a queue nothing would ever `put` to again: the live view
        would show "Connecting..." forever despite a healthy, `(open)`
        websocket. Only the last consumer's `stop_screencast` may actually
        tear anything down.
        """

        cctx = self._require_context(ctx)
        live = self._require_page(cctx, page_id)
        if live.cdp_session is None:
            return
        if live.frame_queue_refs > 0:
            live.frame_queue_refs -= 1
        if live.frame_queue_refs > 0:
            return
        try:
            await live.cdp_session.send("Page.stopScreencast")
            await live.cdp_session.detach()
        except Exception:
            pass  # context may already be closing; best-effort teardown
        live.cdp_session = None
        live.frame_queue = None

    async def dispatch_input(
        self, ctx: ContextRef, event: InputEvent, page_id: str | None = None
    ) -> None:
        cctx = self._require_context(ctx)
        live = self._require_page(cctx, page_id)
        if live.cdp_session is None:
            raise ContextCrashed("live view is not active for this tab")
        method, params = to_cdp_input_params(event)
        await live.cdp_session.send(method, params)

    # --- CdpEndpointCapable (optional capability; see spi.cdp) ---
    # Deliberately NOT subject to LiveViewCapable's "Page/Input only, never
    # Runtime" rule above -- an enable_cdp=True session opts out of
    # Patchright's anti-detection guarantee entirely, by design.

    async def cdp_http_base(self, ctx: ContextRef) -> str | None:
        cctx = self._require_context(ctx)
        return cctx.cdp_http_base
