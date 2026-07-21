"""The one concrete `BrowserDriver` implementation for P0.

Playwright/Patchright objects never leave this module -- everything returned
to callers is a `baas.spi` dataclass. `execute()` is the single dispatch loop
batching a whole `list[Action]` into one `ActionResult`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from patchright.async_api import BrowserContext, Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from baas.driver.aria_parse import parse_aria_snapshot
from baas.driver.process_launcher import ProcessLauncher
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
    ScreenshotAction,
    SelectOptionAction,
    SnapshotAction,
    WaitAction,
)

_REF_CONSUMING = (ClickAction, FillAction, SelectOptionAction, HoverAction)
from baas.spi.egress import EgressPolicy
from baas.spi.errors import ContextCrashed, NavigationTimeout
from baas.spi.health import HealthStatus
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, ContextState
from baas.spi.proxy import ProxyEndpoint
from baas.spi.snapshot import AXSnapshot
from baas.spi.storage_state import LocalStorageEntry, OriginState, StorageState

log = structlog.get_logger(__name__)


@dataclass
class _Live:
    context: BrowserContext
    page: Page
    epoch: int = 0
    alive: bool = True
    death_reason: str | None = None
    block_popups: bool = False
    page_changed: bool = False


def _proxy_settings(proxy: ProxyEndpoint | None) -> dict | None:
    if proxy is None:
        return None
    settings: dict = {"server": f"{proxy.scheme}://{proxy.host}:{proxy.port}"}
    if proxy.username:
        settings["username"] = proxy.username
    if proxy.password:
        settings["password"] = proxy.password
    return settings


def _to_playwright_storage_state(state: StorageState) -> dict:
    return {
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
    }


def _from_playwright_storage_state(raw: dict) -> StorageState:
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

        def _on_context_close() -> None:
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
                isinstance(action, WaitAction) and action.ref is not None
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
                raise NotImplementedError("WaitAction(ref=...) needs P1's ref_cache")
            await asyncio.sleep((action.ms or 0) / 1000)
        elif isinstance(action, ExecuteJsAction):
            result.js_returns.append(await live.page.evaluate(action.script))
        else:
            raise NotImplementedError(
                f"{type(action).__name__} needs P1's ref_cache to resolve refs"
            )

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
