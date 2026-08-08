"""The one concrete `BrowserDriver` implementation.

Playwright/Patchright objects never leave this module -- everything returned
to callers is a `agentpilot.spi` dataclass. `execute()` is the single dispatch loop
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
    FloatRect,
    Locator,
    Page,
    ProxySettings,
)
from patchright.async_api import StorageState as PlaywrightStorageState
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from agentpilot.driver import block_detect, humanize, warmup
from agentpilot.driver.aria_parse import (
    collect_leaf_refs,
    filter_snapshot,
    parse_aria_snapshot,
    prune_to_refs,
)
from agentpilot.driver.dom_fusion_engine import capture_fused_tree
from agentpilot.driver.live_view import (
    SCREENCAST_START_PARAMS,
    parse_screencast_frame,
    to_cdp_input_params,
)
from agentpilot.driver.process_launcher import ProcessLauncher
from agentpilot.driver.ref_cache import RefCache
from agentpilot.egress.policy import apply_baseline
from agentpilot.extraction.extractor import extract
from agentpilot.observability.metrics import (
    context_leak_warnings_total,
    context_task_outcomes_total,
    context_tasks_total,
)
from agentpilot.spi.actions import (
    Action,
    ActionResult,
    ClickAction,
    CloseTabAction,
    ExecuteJsAction,
    ExtractAction,
    FillAction,
    GoBackAction,
    HoverAction,
    ListTabsAction,
    NavigateAction,
    NewTabAction,
    PressAction,
    ScreenshotAction,
    ScrollAction,
    SelectOptionAction,
    SnapshotAction,
    SwitchTabAction,
    TabInfo,
    WaitAction,
)
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import (
    CapacityExhausted,
    ChallengeDetected,
    ContextCrashed,
    NavigationTimeout,
    StaleRefError,
    TabNotFound,
)
from agentpilot.spi.health import ContextHealth, HealthStatus
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState
from agentpilot.spi.proxy import ProxyEndpoint
from agentpilot.spi.snapshot import AXSnapshot, BoundingBox, SnapshotNode
from agentpilot.spi.storage_state import LocalStorageEntry, OriginState, StorageState
from agentpilot.spi.streaming import InputEvent, LiveViewFrame

log = structlog.get_logger(__name__)

_REF_CONSUMING = (ClickAction, FillAction, SelectOptionAction, HoverAction)

_BOUNDING_BOX_TIMEOUT_MS = 3_000
"""Bounded well below Playwright's 30s default -- see `_resolve_ref`'s and
`_apply_viewport_filter`'s docstrings for why an unbounded wait here is a
real observed hang, not a hypothetical one."""

_LIVENESS_TIMEOUT_S = 2.0
"""Upper bound on the CDP liveness/keepalive ping. A responsive context answers
`Page.getLayoutMetrics` in well under this; a wedged renderer hits the bound and
is reported dead so the session layer can reopen it, rather than hanging the
acquire path on an unresponsive Chrome."""

_SETTLE_TIMEOUT_MS = 1_500
"""Upper bound on the best-effort network-idle wait before an opt-in
(`SnapshotAction.settle`) snapshot. Bounded because `networkidle` never fires
on pages with persistent connections (long-poll, websockets, analytics
beacons); on those the wait simply caps out and the snapshot proceeds. A
MutationObserver-based probe that returns early on DOM quiescence (Browser4's
`waitForDOMSettle`) is a later refinement -- this is the cheap first cut."""

_SCROLL_DELTAS: dict[str, tuple[float, float]] = {
    "down": (0, 600),
    "up": (0, -600),
    "right": (600, 0),
    "left": (-600, 0),
}

# --- Behavioral-realism knobs (anti-detection). Real users don't fill an
# input in a single instantaneous DOM write, teleport the pointer to an
# element's exact center, or scroll by a pixel-perfect fixed delta every
# time -- WAFs that score pointer/keystroke telemetry flag exactly that
# regularity. These add human-plausible variance without changing what an
# action ultimately does. All keyed off `random`, seeded per-process, so
# tests that need determinism can `random.seed()`.

_TYPE_DELAY_MS_RANGE = (90, 240)
"""Per-character keystroke delay for `FillAction`, matching the range a
sibling project documented for realistic typing (see CLAUDE.md's
`randomDelayMillis("type")` note)."""

_MOUSE_MOVE_STEPS_RANGE = (8, 18)
"""Intermediate `mouse.move` steps before a click -- Playwright interpolates
a straight line across them, still far more human than a single teleport to
the target center; step count varies so the cadence isn't fixed."""

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


def _jittered_scroll_delta(direction: str) -> tuple[float, float]:
    dx, dy = _SCROLL_DELTAS[direction]
    return _jitter(dx), _jitter(dy)


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
    ref_cache: RefCache = field(default_factory=RefCache)


@dataclass
class _ContextHealth:
    """Per-context health tallies -- Wave 0 instrumentation only. Counters are
    recorded but nothing acts on them yet; a later pass adds leak detection
    (`leak_warnings`) and quality signals (`small_pages`), then a
    `should_retire` policy that rotates a flagged profile (mirrors Browser4's
    `AbstractPrivacyContext`: `privacyLeakWarnings`, `failureRate`,
    `smallPageRate`). A "task" here is one `execute()` batch."""

    tasks: int = 0
    successes: int = 0
    failures: int = 0
    small_pages: int = 0
    """Reserved: batches that returned a suspiciously small/blocked page.
    Populated by a later leak/quality-detection pass, not in Wave 0."""
    leak_warnings: int = 0
    """Reserved: bot-detection signals (captcha/challenge/block). Populated by
    a later leak-detection pass, not in Wave 0."""

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


def _record_ref_metadata(cache: RefCache, node: SnapshotNode) -> None:
    cache.record(node.ref, role=node.role, name=node.name)
    for child in node.children:
        _record_ref_metadata(cache, child)


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
    ) -> None:
        self._launcher = launcher
        self._max_tabs_per_session = max_tabs_per_session
        self._node_id = node_id
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
        identity: IdentityKey,
        profile_dir: Path,
        proxy: ProxyEndpoint | None,
        headful: bool,
        egress: EgressPolicy,
        block_popups: bool = False,
        enable_cdp: bool = False,
        locale: str | None = None,
        timezone_id: str | None = None,
        warmup: bool = False,
        detect_blocks: bool = False,
        user_agent: str | None = None,
        init_script: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
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
        effective_headful = headful and self._launcher.ensure_display()
        if headful and not effective_headful:
            log.info("driver.headful_downgraded_no_display", context=str(profile_dir))

        playwright = await self._launcher.get_playwright()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=not effective_headful,
            no_viewport=True,
            proxy=_proxy_settings(proxy),
            args=launch_args,
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
        if enable_cdp:
            assert cdp_port is not None
            await _wait_for_cdp_ready(cdp_port)
        page = context.pages[0] if context.pages else await context.new_page()

        context_id = str(uuid.uuid4())
        page_id = str(uuid.uuid4())
        cctx = _Context(
            context=context,
            pages={page_id: _Page(page=page)},
            active_page_id=page_id,
            max_tabs=self._max_tabs_per_session,
            block_popups=block_popups,
            cdp_http_base=f"http://127.0.0.1:{cdp_port}" if enable_cdp else None,
            warmup=warmup,
            detect_blocks=detect_blocks,
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
        if cctx.alive:
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
        Runtime, same stealth constraint as `_apply_viewport_filter`) proving
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
        context_tasks_total.inc()

        succeeded = False
        try:
            stale = False
            for action in actions:
                consumes_ref = isinstance(action, _REF_CONSUMING) or (
                    isinstance(action, (WaitAction, ScrollAction)) and action.ref is not None
                )
                if stale and consumes_ref:
                    result.sequence_aborted = True
                    break
                try:
                    pre_url = live.page.url
                    await self._dispatch(cctx, live, action, result)
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
                context_task_outcomes_total.labels(outcome="success").inc()
            else:
                cctx.health.failures += 1
                context_task_outcomes_total.labels(outcome="failure").inc()

    async def _dispatch(
        self, cctx: _Context, live: _Page, action: Action, result: ActionResult
    ) -> None:
        if isinstance(action, NavigateAction):
            try:
                response = await live.page.goto(action.url, timeout=action.timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise NavigationTimeout(str(exc)) from exc
            # Any ref taken before this navigation must not resolve against
            # the new page -- bump the epoch and reset the ref cache the
            # same way SnapshotAction does, rather than relying on every
            # caller to always re-snapshot before reusing a ref. Without
            # this, RefCache's AX_NAME fallback tier could spuriously
            # resolve a stale ref against an unrelated element on the new
            # page instead of correctly raising StaleRefError.
            live.epoch += 1
            live.ref_cache.reset(live.epoch)
            result.verifications.append(f"navigated to {live.page.url}")
            await self._post_navigate(cctx, live, result, response)
        elif isinstance(action, GoBackAction):
            await live.page.go_back()
            live.epoch += 1
            live.ref_cache.reset(live.epoch)
        elif isinstance(action, SnapshotAction):
            if action.settle:
                await self._settle(live)
            live.epoch += 1
            if action.engine == "fusion":
                # CDP DOM/Snapshot/AX fusion -> EnhancedDOMTreeNode with stable
                # backendNodeId identity and cross-step change detection. Refs
                # are `e<backendNodeId>`, resolved by RefCache's backend-id tier.
                tree = await self._capture_fused(live, no_runtime=action.no_runtime)
                live.ref_cache.reset(live.epoch)
                live.ref_cache.record_fused(tree)
                result.fused_trees.append(tree)
            else:
                text = await live.page.aria_snapshot(mode="ai")
                root = parse_aria_snapshot(text, epoch=live.epoch)
                live.ref_cache.reset(live.epoch)
                _record_ref_metadata(live.ref_cache, root)
                if action.viewport_only:
                    root = await self._apply_viewport_filter(live, root)
                root = filter_snapshot(root, roles=action.roles, max_nodes=action.max_nodes)
                if action.with_bbox:
                    # After filtering, not before: only pays for refs that
                    # actually survived viewport/role/max_nodes pruning.
                    await self._annotate_bounding_boxes(live, root)
                result.snapshots.append(AXSnapshot(epoch=live.epoch, root=root))
        elif isinstance(action, ExtractAction):
            # Lazy, once-per-batch: cheap dedicated CDP getter, not tied to a
            # full page.content() fetch -- doesn't add a round trip to
            # non-extract batches (interactive click/fill/etc. sequences),
            # and isn't refetched if a batch requests multiple formats.
            if result.page_title is None:
                result.page_title = await live.page.title()
            html = await live.page.content()
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
            result.screenshots.append(await live.page.screenshot(full_page=action.full_page))
        elif isinstance(action, WaitAction):
            if action.ref is not None:
                await self._resolve_ref(live, action.ref)
            else:
                await asyncio.sleep((action.ms or 0) / 1000)
        elif isinstance(action, ExecuteJsAction):
            result.js_returns.append(await live.page.evaluate(action.script))
        elif isinstance(action, ClickAction):
            pre_click_url = live.page.url
            locator = await self._resolve_ref(live, action.ref)
            if action.all:
                for i in range(await locator.count()):
                    await self._human_click(live, locator.nth(i))
            else:
                await self._human_click(live, locator)
            if live.page.url != pre_click_url:
                result.verifications.append(f"click on {action.ref} navigated to {live.page.url}")
            else:
                result.verifications.append(f"clicked {action.ref}")
        elif isinstance(action, FillAction):
            locator = await self._resolve_ref(live, action.ref)
            await self._human_fill(live, locator, action.text)
            # Read-back grounding: confirm the value actually landed in the
            # field rather than assuming success. Best-effort -- a field that
            # can't report its value must not fail the fill.
            with contextlib.suppress(Exception):
                value = await locator.input_value()
                result.verifications.append(
                    f"filled {action.ref}: field now contains {value[:80]!r}"
                )
        elif isinstance(action, SelectOptionAction):
            locator = await self._resolve_ref(live, action.ref)
            await locator.select_option(action.values)
        elif isinstance(action, HoverAction):
            locator = await self._resolve_ref(live, action.ref)
            await locator.hover()
        elif isinstance(action, PressAction):
            await live.page.keyboard.press(action.key)
        elif isinstance(action, ScrollAction):
            if action.ref is not None:
                locator = await self._resolve_ref(live, action.ref)
                await locator.scroll_into_view_if_needed()
            else:
                dx, dy = _jittered_scroll_delta(action.direction)
                await live.page.mouse.wheel(dx, dy)
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

        if cctx.warmup:
            # STEALTH timing: warm-up only runs for protected targets. Never
            # let the flourish fail the navigation it follows.
            with contextlib.suppress(Exception):
                await warmup.warm_up(live.page, humanize.STEALTH, wait_abck=cctx.detect_blocks)

        if cctx.detect_blocks:
            html: str | None = None
            with contextlib.suppress(Exception):
                html = await live.page.content()
            verdict = block_detect.classify_page(html=html, url=live.page.url, status=status)
            weight = block_detect.warning_weight(verdict)
            if weight:
                cctx.health.leak_warnings += weight
                context_leak_warnings_total.labels(reason=verdict.value).inc()
            if block_detect.is_blocked(verdict):
                raise ChallengeDetected(
                    f"{verdict.value} at {live.page.url}", verdict=verdict.value, weight=weight
                )
            if verdict is not block_detect.Verdict.OK:
                result.verifications.append(f"warning: page classified {verdict.value}")
            return

        # Status-only fallback (unchanged) when detect_blocks is off.
        reason = _navigation_leak_reason(status)
        if reason is not None:
            cctx.health.leak_warnings += 1
            context_leak_warnings_total.labels(reason=reason).inc()
            result.verifications.append(
                f"warning: navigation returned {reason} -- possible block or bot challenge"
            )

    async def _settle(self, live: _Page) -> None:
        """Best-effort, bounded wait for the page to reach network-idle before
        an opt-in (`SnapshotAction.settle`) snapshot, so the agent perceives a
        stable page across steps. Swallows everything -- including the expected
        timeout on pages that never go idle -- because a failed settle must
        never fail the snapshot it precedes."""

        with contextlib.suppress(Exception):
            await live.page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT_MS)

    async def _resolve_ref(self, live: _Page, ref: str) -> Locator:
        """Resolves via `RefCache`, then enforces visibility: a ref that
        resolves to a zero-size box (`display:none`, collapsed, off-canvas)
        is treated the same as a ref that failed to resolve at all -- both
        mean "don't dispatch a click/fill nobody would see land".

        `bounding_box()`'s own actionability wait can take its full default
        30s to time out when a ref genuinely can't be resolved (observed
        directly: `aria-ref=` locators sometimes hang the entire default
        timeout rather than failing fast) -- a bounded timeout here means
        that failure surfaces as a typed `StaleRefError` in ~3s instead of
        stalling the whole `execute()` batch for 30s."""

        locator = await live.ref_cache.resolve(live.page, ref)
        try:
            box = await locator.bounding_box(timeout=_BOUNDING_BOX_TIMEOUT_MS)
        except PlaywrightTimeoutError as exc:
            raise StaleRefError(ref, epoch_superseded=False) from exc
        if box is None or box["width"] <= 0 or box["height"] <= 0:
            raise StaleRefError(ref, epoch_superseded=False)
        return locator

    async def _human_click(self, live: _Page, locator: Locator) -> None:
        """Approach the target over several interpolated `mouse.move` steps
        to a jittered point *inside* the element, then click -- versus
        Playwright's default single teleport to the exact center. Best
        effort: if the box can't be resolved quickly, fall straight through
        to a plain `.click()` (which does its own actionability wait) rather
        than failing the action for the sake of the flourish."""

        try:
            box = await locator.bounding_box(timeout=_BOUNDING_BOX_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            box = None
        if box is not None and box["width"] > 0 and box["height"] > 0:
            tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            with contextlib.suppress(Exception):
                await live.page.mouse.move(tx, ty, steps=random.randint(*_MOUSE_MOVE_STEPS_RANGE))
        await locator.click()

    async def _human_fill(self, live: _Page, locator: Locator, text: str) -> None:
        """Clear, then type character-by-character with a randomized delay
        *between* keystrokes -- versus `locator.fill()`'s single instantaneous
        DOM write, which is a strong automation tell to keystroke-timing
        telemetry. `fill("")` clears any existing value first; the final
        field value is identical to what `fill(text)` would have produced."""

        await locator.fill("")
        if not text:
            return
        await locator.focus()
        for ch in text:
            await live.page.keyboard.type(ch)
            await asyncio.sleep(random.uniform(*_TYPE_DELAY_MS_RANGE) / 1000)

    async def _capture_fused(self, live: _Page, *, no_runtime: bool):
        """Capture a fused `EnhancedDOMTreeNode` via the CDP fusion engine,
        reusing the page's live CDP session when one exists (the live-view
        screencast session) or creating a short-lived one otherwise -- same
        acquisition pattern as `_apply_viewport_filter`. `no_runtime` (from the
        UI stealth tier) forbids the engine's only Runtime call."""

        cdp = live.cdp_session
        owns_session = cdp is None
        if cdp is None:
            cdp = await live.page.context.new_cdp_session(live.page)
        try:
            return await capture_fused_tree(cdp, no_runtime=no_runtime)
        finally:
            if owns_session:
                with contextlib.suppress(Exception):
                    await cdp.detach()

    async def _apply_viewport_filter(self, live: _Page, root: SnapshotNode) -> SnapshotNode:
        """Best-effort viewport clipping via CDP `Page.getLayoutMetrics`
        (Page domain -- never Runtime, same constraint as live-view) rather
        than Playwright's own `viewport_size()`, since `open()` launches with
        `no_viewport=True` and would report `None` otherwise. Leaf-ref
        bounding boxes are resolved concurrently (`asyncio.gather`) so CDP
        pipelines the round trips instead of paying N sequential ones --
        the whole reason `viewport_only` exists is to shrink an otherwise
        enormous snapshot, so serializing this would defeat the point.
        Each `bounding_box()` gets `_BOUNDING_BOX_TIMEOUT_MS`, not
        Playwright's 30s default -- observed directly during testing:
        `aria-ref=` locators occasionally hang the full default timeout
        rather than failing fast, which would otherwise make a single
        snapshot call take 30s per unresolvable ref."""

        leaf_refs = collect_leaf_refs(root)
        if not leaf_refs:
            return root
        cdp = live.cdp_session
        owns_session = cdp is None
        if cdp is None:
            cdp = await live.page.context.new_cdp_session(live.page)
        try:
            metrics = await cdp.send("Page.getLayoutMetrics")
            viewport = metrics["cssVisualViewport"]
            vw, vh = viewport["clientWidth"], viewport["clientHeight"]
            boxes_by_ref = await self._resolve_bounding_boxes(live, leaf_refs)
        finally:
            if owns_session:
                with contextlib.suppress(Exception):
                    await cdp.detach()

        def _in_viewport(box: FloatRect) -> bool:
            return (
                box["x"] + box["width"] > 0
                and box["x"] < vw
                and box["y"] + box["height"] > 0
                and box["y"] < vh
            )

        # Three distinct outcomes from `bounding_box()`, not two: a `dict` is
        # checked against the viewport; `None` is Playwright's own "confirmed
        # not visible/detached" signal, so that's a real prune; an
        # *exception* means resolution failed for some transient reason
        # (slow layout, momentary CDP hiccup) -- unrelated to whether the
        # element is actually on-screen, so it fails open (kept) rather than
        # silently discarding real content because one lookup was flaky.
        visible = {
            ref
            for ref, box in boxes_by_ref.items()
            if (isinstance(box, dict) and _in_viewport(box)) or isinstance(box, Exception)
        }
        return prune_to_refs(root, visible)

    async def _resolve_bounding_boxes(
        self, live: _Page, refs: list[str]
    ) -> dict[str, FloatRect | None | BaseException]:
        """Concurrently resolves bounding boxes for `refs` via CDP-backed
        Playwright locators, so callers pipeline the round trips instead of
        paying N sequential ones -- shared by `_apply_viewport_filter` (which
        interprets `None`/exception itself) and `_annotate_bounding_boxes`
        (which only cares about the successful `dict` case)."""

        if not refs:
            return {}
        boxes = await asyncio.gather(
            *(
                live.page.locator(f"aria-ref={ref}").bounding_box(timeout=_BOUNDING_BOX_TIMEOUT_MS)
                for ref in refs
            ),
            return_exceptions=True,
        )
        return dict(zip(refs, boxes, strict=True))

    async def _annotate_bounding_boxes(self, live: _Page, root: SnapshotNode) -> None:
        """Populates `SnapshotNode.bbox` in place for every leaf ref in the
        (already-filtered) tree -- `agentpilot.agent`'s step loop uses this to
        give the LLM coordinate grounding alongside a screenshot. Best
        effort: a ref whose box can't be resolved (detached, transient CDP
        hiccup) is simply left with `bbox=None`, the same fail-open posture
        `_apply_viewport_filter` uses for lookup failures."""

        leaf_refs = collect_leaf_refs(root)
        boxes_by_ref = await self._resolve_bounding_boxes(live, leaf_refs)

        def walk(node: SnapshotNode) -> None:
            if not node.children and node.ref:
                box = boxes_by_ref.get(node.ref)
                if isinstance(box, dict):
                    node.bbox = BoundingBox(
                        x=box["x"], y=box["y"], width=box["width"], height=box["height"]
                    )
            for child in node.children:
                walk(child)

        walk(root)

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
