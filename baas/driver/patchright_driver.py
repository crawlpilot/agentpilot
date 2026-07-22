"""The one concrete `BrowserDriver` implementation.

Playwright/Patchright objects never leave this module -- everything returned
to callers is a `baas.spi` dataclass. `execute()` is the single dispatch loop
batching a whole `list[Action]` into one `ActionResult`. P1 adds real
dispatch for the interaction verbs (via `driver/ref_cache.py`), snapshot
token-budget filtering (`roles`/`max_nodes`/`viewport_only`), and the
view-only live-view screencast (`LiveViewCapable`, at the bottom of this
class).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, assert_never, cast

import structlog
from patchright.async_api import BrowserContext, CDPSession, Locator, Page, ProxySettings
from patchright.async_api import StorageState as PlaywrightStorageState
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from baas.driver.aria_parse import (
    collect_leaf_refs,
    filter_snapshot,
    parse_aria_snapshot,
    prune_to_refs,
)
from baas.driver.live_view import (
    SCREENCAST_START_PARAMS,
    parse_screencast_frame,
    to_cdp_input_params,
)
from baas.driver.process_launcher import ProcessLauncher
from baas.driver.ref_cache import RefCache
from baas.egress.policy import apply_baseline
from baas.extraction.extractor import extract
from baas.spi.actions import (
    Action,
    ActionResult,
    ClickAction,
    ExecuteJsAction,
    ExtractAction,
    FillAction,
    GoBackAction,
    HoverAction,
    NavigateAction,
    PressAction,
    ScreenshotAction,
    ScrollAction,
    SelectOptionAction,
    SnapshotAction,
    WaitAction,
)
from baas.spi.egress import EgressPolicy
from baas.spi.errors import ContextCrashed, NavigationTimeout, StaleRefError
from baas.spi.health import HealthStatus
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, ContextState
from baas.spi.proxy import ProxyEndpoint
from baas.spi.snapshot import AXSnapshot, SnapshotNode
from baas.spi.storage_state import LocalStorageEntry, OriginState, StorageState
from baas.spi.streaming import InputEvent, LiveViewFrame

log = structlog.get_logger(__name__)

_REF_CONSUMING = (ClickAction, FillAction, SelectOptionAction, HoverAction)

_SCROLL_DELTAS: dict[str, tuple[float, float]] = {
    "down": (0, 600),
    "up": (0, -600),
    "right": (600, 0),
    "left": (-600, 0),
}


@dataclass
class _Live:
    context: BrowserContext
    page: Page
    epoch: int = 0
    alive: bool = True
    death_reason: str | None = None
    block_popups: bool = False
    page_changed: bool = False
    cdp_session: CDPSession | None = None
    frame_queue: asyncio.Queue[LiveViewFrame] | None = None
    ref_cache: RefCache = field(default_factory=RefCache)


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
                        {"name": entry.name, "value": entry.value}
                        for entry in origin.local_storage
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
    def __init__(self, launcher: ProcessLauncher) -> None:
        self._launcher = launcher
        self._live: dict[str, _Live] = {}

    def _require_live(self, ctx: ContextRef) -> _Live:
        live = self._live.get(ctx.context_id)
        if live is None or not live.alive:
            raise ContextCrashed(f"context {ctx.context_id} is not alive")
        return live

    async def open(
        self,
        identity: IdentityKey,
        profile_dir: Path,
        proxy: ProxyEndpoint | None,
        headful: bool,
        egress: EgressPolicy,
    ) -> ContextRef:
        apply_baseline(egress)
        if headful:
            self._launcher.ensure_xvfb()

        playwright = await self._launcher.get_playwright()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=not headful,
            no_viewport=True,
            proxy=_proxy_settings(proxy),
        )
        page = context.pages[0] if context.pages else await context.new_page()

        live = _Live(context=context, page=page)
        context_id = str(uuid.uuid4())

        def _on_crash(_page: Page) -> None:
            live.alive = False
            live.death_reason = "page_crash"
            log.warning("driver.page_crashed", context_id=context_id)

        def _on_context_close(_context: BrowserContext) -> None:
            live.alive = False
            live.death_reason = live.death_reason or "context_closed"

        def _on_new_page(new_page: Page) -> None:
            if live.block_popups:
                asyncio.create_task(new_page.close())
                return
            live.page = new_page
            live.page_changed = True

        page.on("crash", _on_crash)
        context.on("close", _on_context_close)
        context.on("page", _on_new_page)

        self._live[context_id] = live
        return ContextRef(
            context_id=context_id,
            identity=identity,
            state=ContextState.ACTIVE,
            pid=None,  # resolved via cdp_patches on demand in P1, when interaction actions need it
            node_id="local",
        )

    async def close(self, ctx: ContextRef) -> None:
        live = self._live.pop(ctx.context_id, None)
        if live is None:
            return
        if live.alive:
            await live.context.close()

    async def execute(self, ctx: ContextRef, actions: list[Action]) -> ActionResult:
        """Dispatches the whole batch, aborting early if the page navigated
        out from under a ref-consuming action.

        `Action.terminates_sequence` is caller-facing metadata (e.g. an SDK
        deciding whether to keep queuing actions client-side); it is not the
        enforcement mechanism here. The actual guard is the runtime URL diff:
        a `NavigateAction` immediately followed by `SnapshotAction`/
        `ExtractAction` is the normal P0 pattern and must not abort, but once
        P1's ref-consuming actions (Click/Fill/...) land, any of them
        following an unnoticed navigation -- deliberate or not -- would act
        on stale refs, so those are the ones that trip `sequence_aborted`.
        """

        live = self._require_live(ctx)
        result = ActionResult(page_changed=live.page_changed)
        live.page_changed = False

        stale = False
        for action in actions:
            consumes_ref = isinstance(action, _REF_CONSUMING) or (
                isinstance(action, (WaitAction, ScrollAction)) and action.ref is not None
            )
            if stale and consumes_ref:
                result.sequence_aborted = True
                break
            pre_url = live.page.url
            await self._dispatch(live, action, result)
            if live.page.url != pre_url:
                stale = True
        return result

    async def _dispatch(self, live: _Live, action: Action, result: ActionResult) -> None:
        if isinstance(action, NavigateAction):
            try:
                await live.page.goto(action.url, timeout=action.timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise NavigationTimeout(str(exc)) from exc
        elif isinstance(action, GoBackAction):
            await live.page.go_back()
        elif isinstance(action, SnapshotAction):
            live.epoch += 1
            text = await live.page.aria_snapshot(mode="ai")
            root = parse_aria_snapshot(text, epoch=live.epoch)
            live.ref_cache.reset(live.epoch)
            _record_ref_metadata(live.ref_cache, root)
            if action.viewport_only:
                root = await self._apply_viewport_filter(live, root)
            root = filter_snapshot(root, roles=action.roles, max_nodes=action.max_nodes)
            result.snapshots.append(AXSnapshot(epoch=live.epoch, root=root))
        elif isinstance(action, ExtractAction):
            html = await live.page.content()
            result.extracts.append(
                extract(html, format=action.format, main_content=action.main_content)
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
            locator = await self._resolve_ref(live, action.ref)
            if action.all:
                for i in range(await locator.count()):
                    await locator.nth(i).click()
            else:
                await locator.click()
        elif isinstance(action, FillAction):
            locator = await self._resolve_ref(live, action.ref)
            await locator.fill(action.text)
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
                dx, dy = _SCROLL_DELTAS[action.direction]
                await live.page.mouse.wheel(dx, dy)
        else:
            assert_never(action)

    async def _resolve_ref(self, live: _Live, ref: str) -> Locator:
        """Resolves via `RefCache`, then enforces visibility: a ref that
        resolves to a zero-size box (`display:none`, collapsed, off-canvas)
        is treated the same as a ref that failed to resolve at all -- both
        mean "don't dispatch a click/fill nobody would see land"."""

        locator = await live.ref_cache.resolve(live.page, ref)
        box = await locator.bounding_box()
        if box is None or box["width"] <= 0 or box["height"] <= 0:
            raise StaleRefError(ref, epoch_superseded=False)
        return locator

    async def _apply_viewport_filter(self, live: _Live, root: SnapshotNode) -> SnapshotNode:
        """Best-effort viewport clipping via CDP `Page.getLayoutMetrics`
        (Page domain -- never Runtime, same constraint as live-view) rather
        than Playwright's own `viewport_size()`, since `open()` launches with
        `no_viewport=True` and would report `None` otherwise. Leaf-ref
        bounding boxes are resolved concurrently (`asyncio.gather`) so CDP
        pipelines the round trips instead of paying N sequential ones --
        the whole reason `viewport_only` exists is to shrink an otherwise
        enormous snapshot, so serializing this would defeat the point."""

        leaf_refs = collect_leaf_refs(root)
        if not leaf_refs:
            return root
        cdp = live.cdp_session
        owns_session = cdp is None
        if cdp is None:
            cdp = await live.context.new_cdp_session(live.page)
        try:
            metrics = await cdp.send("Page.getLayoutMetrics")
            viewport = metrics["cssVisualViewport"]
            vw, vh = viewport["clientWidth"], viewport["clientHeight"]
            boxes = await asyncio.gather(
                *(live.page.locator(f"aria-ref={ref}").bounding_box() for ref in leaf_refs),
                return_exceptions=True,
            )
        finally:
            if owns_session:
                with contextlib.suppress(Exception):
                    await cdp.detach()

        visible = {
            ref
            for ref, box in zip(leaf_refs, boxes, strict=True)
            if isinstance(box, dict)
            and box["x"] + box["width"] > 0
            and box["x"] < vw
            and box["y"] + box["height"] > 0
            and box["y"] < vh
        }
        return prune_to_refs(root, visible)

    async def export_state(self, ctx: ContextRef) -> StorageState:
        live = self._require_live(ctx)
        raw = await live.context.storage_state()
        return _from_playwright_storage_state(raw)

    async def restore_state(self, ctx: ContextRef, state: StorageState) -> None:
        live = self._require_live(ctx)
        await live.context.set_storage_state(_to_playwright_storage_state(state))

    async def health(self, ctx: ContextRef) -> HealthStatus:
        live = self._live.get(ctx.context_id)
        if live is None:
            return HealthStatus(alive=False, reason="context_not_found")
        return HealthStatus(alive=live.alive, reason=live.death_reason)

    # --- LiveViewCapable (optional capability; see spi.streaming) ---
    # Page and Input domains only -- never Runtime, to preserve Patchright's
    # anti-leak guarantee (see baas/driver/live_view.py's module docstring).

    async def start_screencast(self, ctx: ContextRef) -> asyncio.Queue[LiveViewFrame]:
        live = self._require_live(ctx)
        if live.frame_queue is not None:
            return live.frame_queue

        queue: asyncio.Queue[LiveViewFrame] = asyncio.Queue(maxsize=2)
        cdp = await live.context.new_cdp_session(live.page)

        async def _ack_and_enqueue(params: dict[str, Any]) -> None:
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            if queue.full():
                queue.get_nowait()  # live view wants the latest frame, not a backlog
            queue.put_nowait(parse_screencast_frame(params))

        cdp.on("Page.screencastFrame", lambda params: asyncio.create_task(_ack_and_enqueue(params)))
        await cdp.send("Page.startScreencast", SCREENCAST_START_PARAMS)

        live.cdp_session = cdp
        live.frame_queue = queue
        return queue

    async def stop_screencast(self, ctx: ContextRef) -> None:
        live = self._require_live(ctx)
        if live.cdp_session is None:
            return
        try:
            await live.cdp_session.send("Page.stopScreencast")
            await live.cdp_session.detach()
        except Exception:
            pass  # context may already be closing; best-effort teardown
        live.cdp_session = None
        live.frame_queue = None

    async def dispatch_input(self, ctx: ContextRef, event: InputEvent) -> None:
        live = self._require_live(ctx)
        if live.cdp_session is None:
            raise ContextCrashed("live view is not active for this context")
        method, params = to_cdp_input_params(event)
        await live.cdp_session.send(method, params)
