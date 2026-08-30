"""The public entry point: `Browser` and `BrowserSession`.

Before this, driving a browser meant calling `run_ephemeral_scrape()` or
`open_interactive_session()` -- free functions taking 18 and 20 keyword
arguments respectively, every one of which the caller had to construct itself:
a driver, a registry, a proxy pinner, a vault, a profiles root, lease TTLs. The
only code that knew how to assemble them was `gateway.wiring`, 500+ lines that
sits *above* those functions in the layering and so cannot be reused as a
library entry point. In practice the browser was reachable only through the HTTP
gateway (plan D2, D9).

`Browser` is that assembly, with every part optional:

    async with Browser() as browser:
        async with browser.session() as page:
            await page.navigate("https://example.com")
            print(await page.markdown())

Everything defaults to something inert-but-working -- a real Chrome driver, an
in-process registry, no proxies, no prototype catalog, no extensions -- so a
crawler passes nothing. The platform passes its Redis-backed registry, its
tenant-aware proxy provider and its extension registry, and gets the same object.

Batching stays the transport. Each method composes a batch and dispatches it in
one call; convenience methods are sugar over `execute()`, never a second code
path. A caller that wants several actions in one round trip uses `execute()`
directly.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from crawlpilot.config import DEFAULTS, BrowserConfig
from crawlpilot.extensions import Extension, ExtensionRegistry
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy import NullPrototypes, PrototypeProvider
from crawlpilot.session.ephemeral import run_ephemeral_scrape
from crawlpilot.session.interactive import (
    InteractiveSession,
    execute_on_session,
    open_interactive_session,
    release_interactive_session,
)
from crawlpilot.session.registry import Registry, RegistryProtocol
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.actions import ActionResult
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode
from crawlpilot.spi.driver import BrowserDriver
from crawlpilot.spi.egress import LIBRARY_EGRESS, EgressPolicy
from crawlpilot.spi.scrape import Document, ScrapeOptions
from crawlpilot.tiers import Tier

DEFAULT_LEASE_TTL_SECONDS = 300.0


class BrowserSession:
    """One open browser context, driven action by action.

    Not constructed directly -- `Browser.session()` yields one and releases it.
    """

    def __init__(self, browser: Browser, session: InteractiveSession) -> None:
        self._browser = browser
        self._session = session

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def tier(self) -> str:
        return self._session.tier

    # ------------------------------------------------------------- transport

    async def execute(
        self, actions: Sequence[spi_actions.Action], *, page_id: str | None = None
    ) -> ActionResult:
        """Dispatch a batch. The escape hatch every method below is sugar over --
        several actions in one round trip rather than one call each."""

        return await execute_on_session(
            self._session,
            list(actions),
            registry=self._browser.registry,
            driver=self._browser.driver,
            page_id=page_id,
        )

    # ------------------------------------------------------------ navigation

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "load",
        timeout_ms: int = 30_000,
        referer: str | None = None,
    ) -> ActionResult:
        return await self.execute(
            [
                spi_actions.NavigateAction(
                    url=url,
                    wait_until=wait_until,  # type: ignore[arg-type]
                    timeout_ms=timeout_ms,
                    referer=referer,
                )
            ]
        )

    async def go_back(self) -> ActionResult:
        return await self.execute([spi_actions.GoBackAction()])

    # ----------------------------------------------------------- interaction

    async def click(self, ref: str, *, all: bool = False) -> ActionResult:
        return await self.execute([spi_actions.ClickAction(ref=ref, all=all)])

    async def fill(self, ref: str, text: str, *, clear: bool = True) -> ActionResult:
        """Type into a field. `clear=False` appends to what is already there."""

        return await self.execute(
            [spi_actions.FillAction(ref=ref, text=text, clear=clear)]
        )

    async def select_option(self, ref: str, *values: str) -> ActionResult:
        return await self.execute(
            [spi_actions.SelectOptionAction(ref=ref, values=list(values))]
        )

    async def hover(self, ref: str) -> ActionResult:
        return await self.execute([spi_actions.HoverAction(ref=ref)])

    async def press(self, key: str) -> ActionResult:
        return await self.execute([spi_actions.PressAction(key=key)])

    async def scroll(
        self, direction: str = "down", *, pages: float = 1.0, ref: str | None = None
    ) -> ActionResult:
        return await self.execute(
            [spi_actions.ScrollAction(direction=direction, pages=pages, ref=ref)]  # type: ignore[arg-type]
        )

    async def wait(self, ms: int, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.WaitAction(ms=ms, ref=ref)])

    async def send_keys(self, keys: str) -> ActionResult:
        """A key or shortcut (`'Escape'`, `'Control+a'`) to whatever has focus."""

        return await self.execute([spi_actions.SendKeysAction(keys=keys)])

    async def find_text(self, text: str) -> ActionResult:
        """Scroll to the first occurrence of `text`."""

        return await self.execute([spi_actions.FindTextAction(text=text)])

    async def upload_file(self, ref: str, path: str | Path) -> ActionResult:
        """Attach a local file to a file input, without a file chooser."""

        return await self.execute(
            [spi_actions.UploadFileAction(ref=ref, path=str(path))]
        )

    async def execute_js(self, script: str) -> Any:
        result = await self.execute([spi_actions.ExecuteJsAction(script=script)])
        return result.js_returns[0] if result.js_returns else None

    # ----------------------------------------------------------------- queries
    #
    # These return their answer directly rather than an `ActionResult` whose
    # `readouts[0]` the caller has to index into -- the same reason `extract()`
    # returns a string.

    async def dropdown_options(self, ref: str) -> str:
        """The options of a `<select>`, read off the last snapshot."""

        result = await self.execute([spi_actions.DropdownOptionsAction(ref=ref)])
        return result.readouts[0] if result.readouts else ""

    async def search_page(self, pattern: str, *, regex: bool = False, **kwargs: Any) -> str:
        """Grep the rendered page text. Cheap; no model, no full observation."""

        result = await self.execute(
            [spi_actions.SearchPageAction(pattern=pattern, regex=regex, **kwargs)]
        )
        return result.readouts[0] if result.readouts else ""

    async def find_elements(
        self, selector: str, *, attributes: Sequence[str] = (), **kwargs: Any
    ) -> str:
        """Query the DOM by CSS selector; returns tags, text and attributes."""

        result = await self.execute(
            [
                spi_actions.FindElementsAction(
                    selector=selector, attributes=list(attributes), **kwargs
                )
            ]
        )
        return result.readouts[0] if result.readouts else ""

    # -------------------------------------------------------------------- tabs

    async def new_tab(self, url: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.NewTabAction(url=url)])

    async def switch_tab(self, page_id: str) -> ActionResult:
        return await self.execute([spi_actions.SwitchTabAction(page_id=page_id)])

    async def close_tab(self, page_id: str) -> ActionResult:
        return await self.execute([spi_actions.CloseTabAction(page_id=page_id)])

    async def list_tabs(self) -> list[spi_actions.TabInfo]:
        result = await self.execute([spi_actions.ListTabsAction()])
        return result.tabs[0] if result.tabs else []

    # ------------------------------------------------------------------- tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ActionResult:
        """Dispatch a tool by name, as an LLM would call it.

        `browser_tools()` and the `to_anthropic` / `to_openai` / `to_mcp`
        adapters hand a caller tool *definitions*; this is the other half, and
        without it every consumer had to write the same
        name-and-dict → validate → `spi.actions` → `execute()` loop themselves
        (which is exactly what `agentpilot.agent.actions` does today).

        Arguments are validated against the tool's own schema before dispatch, so
        a malformed call is a `ValidationError` naming the field rather than a
        `TypeError` from somewhere inside the driver.
        """

        from crawlpilot.tools import browser_tools  # noqa: PLC0415

        spec = browser_tools().get(name)
        if spec is None:
            raise ValueError(f"no such tool {name!r}")
        if spec.agent_fields is None:
            raise ValueError(
                f"tool {name!r} is not callable this way: it is wire-only "
                "(security-sensitive, or it needs arguments a caller must supply "
                "explicitly). Build the action and pass it to execute()."
            )
        parsed = spec.agent_model().model_validate({"type": spec.name, **(arguments or {})})
        return await self.execute([spec.from_model(parsed)])

    # --------------------------------------------------------------- content

    async def extract(self, fmt: str = "markdown", *, main_content: bool = True) -> str:
        """One extraction, returned directly rather than as an index into
        `ActionResult.extracts` -- the positional correlation that made content
        awkward to reach before (plan D6)."""

        result = await self.execute(
            [
                spi_actions.ExtractAction(
                    format=fmt,  # type: ignore[arg-type]
                    main_content=main_content,
                )
            ]
        )
        return result.extracts[0] if result.extracts else ""

    async def markdown(self, *, main_content: bool = True) -> str:
        return await self.extract("markdown", main_content=main_content)

    async def html(self) -> str:
        return await self.extract("html", main_content=False)

    async def text(self, *, main_content: bool = True) -> str:
        return await self.extract("text", main_content=main_content)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        result = await self.execute([spi_actions.ScreenshotAction(full_page=full_page)])
        return result.screenshots[0] if result.screenshots else b""

    async def snapshot(self, *, settle: bool = True) -> EnhancedDOMTreeNode | None:
        """The fused DOM tree -- what an agent reads to find element refs.

        Settles first by default. A snapshot is an act of perception, and
        capturing a page whose frames and scripts have not finished is how you
        get a tree that is missing the element you were about to act on --
        cross-origin iframes in particular attach late, so an unsettled capture
        routinely misses them entirely. `settle=False` for the cheap read when
        you know the page is already still.
        """

        result = await self.execute([spi_actions.SnapshotAction(settle=settle)])
        return result.fused_trees[0] if result.fused_trees else None


class Browser:
    """A configured browser platform. Construct one, open sessions from it."""

    def __init__(
        self,
        config: BrowserConfig | None = None,
        *,
        driver: BrowserDriver | None = None,
        registry: RegistryProtocol | None = None,
        proxy_pinner: ProxyPinner | None = None,
        prototype_provider: PrototypeProvider | None = None,
        extensions: Sequence[Extension] = (),
        profiles_root: Path | None = None,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        egress: EgressPolicy | None = None,
        executable_path: str | Path | None = None,
        channel: str | None = None,
        headless: bool | None = None,
        cdp_url: str | None = None,
    ) -> None:
        """The launch arguments are flat rather than requiring a `BrowserConfig`.

        `executable_path` / `channel` / `headless` / `cdp_url` are what a user
        actually reaches for -- "use my Chrome", "use the browser in that
        container" -- and making them assemble a nested config object to say so
        is the difference between a one-liner and a paragraph. They override the
        matching fields on `config.launch`, so a platform that builds its config
        from the environment still works unchanged.
        """

        base = config or DEFAULTS
        self.config = replace(
            base,
            launch=replace(
                base.launch,
                **{
                    k: v
                    for k, v in (
                        ("executable_path", str(executable_path) if executable_path else None),
                        ("channel", channel),
                        ("headless", headless),
                        ("cdp_url", cdp_url),
                    )
                    if v is not None
                },
            ),
        )
        self.lease_ttl_seconds = lease_ttl_seconds
        self.egress = egress if egress is not None else LIBRARY_EGRESS
        """Network fence for the browser. Defaults to *not* fencing: enforcement
        is container-wide `iptables`, which a library has no business inserting
        into someone's host firewall, and which would block the local dev server
        they are most likely pointing at. The service passes `EgressPolicy()`."""

        # In-process by default. Correct for one process; a multi-worker
        # deployment injects a shared registry (see `policy.StateStore`'s
        # docstring for why that distinction matters).
        self.registry: RegistryProtocol = registry or Registry()
        self.proxy_pinner = proxy_pinner
        self.prototype_provider: PrototypeProvider = prototype_provider or NullPrototypes()
        self.extensions = (
            extensions
            if isinstance(extensions, ExtensionRegistry)
            else ExtensionRegistry(extensions)
        )

        # A temp dir the browser owns and removes on close, unless the caller
        # supplies one. Warm identities only survive across runs when profiles
        # outlive the process, so a caller that wants returning-visitor
        # behaviour passes `profiles_root`; a one-shot crawler should not have
        # to think about it, and should not silently litter the home directory.
        self._owns_profiles_root = profiles_root is None
        self.profiles_root = profiles_root or Path(tempfile.mkdtemp(prefix="agentpilot-profiles-"))

        self._launcher: Any = None
        self._driver = driver
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    @property
    def driver(self) -> BrowserDriver:
        """The concrete driver, built on first use.

        Deferred so that constructing a `Browser` imports no Chrome machinery:
        `crawlpilot.driver` pulls Patchright, which the Chrome-free deployments
        deliberately do not install.
        """

        if self._driver is None:
            from crawlpilot.driver.patchright_driver import PatchrightDriver  # noqa: PLC0415
            from crawlpilot.driver.process_launcher import ProcessLauncher  # noqa: PLC0415

            self._launcher = ProcessLauncher()
            self._driver = PatchrightDriver(
                self._launcher,
                block_hooks=self.extensions.blocks,
                launch=self.config.launch,
            )
        return self._driver

    @classmethod
    def from_system_chrome(cls, **kwargs: Any) -> Browser:
        """A `Browser` pinned to the Google Chrome installed on this machine.

        The same thing the default resolution order arrives at, said explicitly:
        it fails loudly here if Chrome is absent, rather than quietly falling
        through to a bundled Chromium. Useful when the *point* is real Chrome --
        a site that fingerprints the browser build, or a debugging session you
        want to compare against your own browser.
        """

        from crawlpilot.driver.browser_discovery import (  # noqa: PLC0415
            INSTALL_HINT,
            BrowserNotFound,
            find_system_browser,
        )

        executable = find_system_browser()
        if executable is None:
            raise BrowserNotFound(f"no system Chrome found. {INSTALL_HINT}")
        return cls(executable_path=executable, **kwargs)

    async def __aenter__(self) -> Browser:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._launcher is not None:
            await self._launcher.close()
        if self._owns_profiles_root:
            shutil.rmtree(self.profiles_root, ignore_errors=True)

    # -------------------------------------------------------------- sessions

    @asynccontextmanager
    async def session(
        self,
        *,
        identity: str | None = None,
        domain: str = "default",
        tier: Tier | str = "auto",
        headful: bool = False,
        enable_cdp: bool = False,
        locale: str | None = None,
        timezone_id: str | None = None,
    ) -> AsyncIterator[BrowserSession]:
        """Open a session, yield it, always release it.

        `identity` defaults to a fresh throwaway scope whose profile dir is
        deleted on teardown -- a cookie-less first-visit browser, which is what
        a one-shot crawl wants. Pass one to get the opposite: stickiness across
        calls, so repeat visits reuse a profile, a pinned proxy and a
        fingerprint and read as a returning visitor. It is an opaque scope
        handle, never a domain (plan D10).
        """

        name = identity or f"session-{uuid.uuid4().hex}"
        session = await open_interactive_session(
            session_id=f"s-{uuid.uuid4().hex}",
            scope="local",
            domain=domain,
            name=name,
            tier=str(tier),
            headful=headful,
            block_popups=False,
            enable_cdp=enable_cdp,
            registry=self.registry,
            driver=self.driver,
            profiles_root=self.profiles_root,
            proxy_pinner=self.proxy_pinner,
            vault=None,
            lease_ttl_seconds=self.lease_ttl_seconds,
            locale=locale,
            timezone_id=timezone_id,
            browser_config=self.config,
            prototype_provider=self.prototype_provider,
            egress=self.egress,
        )
        try:
            yield BrowserSession(self, session)
        finally:
            await release_interactive_session(
                session, registry=self.registry, driver=self.driver, vault=None
            )

    # ------------------------------------------------------------ one-shot

    async def scrape(
        self,
        url: str,
        *,
        formats: Sequence[str] = ("markdown",),
        tier: Tier | str = "auto",
        identity: str | None = None,
        options: ScrapeOptions | None = None,
    ) -> Document:
        """Fetch one page and tear the context down immediately.

        Distinct from `session()`: that keeps a context alive across many calls;
        this mints an identity, runs one batch, and evicts -- the right shape
        for crawling a list of URLs where each page is independent.
        """

        opts = options or ScrapeOptions(formats=tuple(formats))  # type: ignore[arg-type]
        document, _screenshot = await run_ephemeral_scrape(
            scope="local",
            domain=_host_of(url),
            url=url,
            options=opts,
            registry=self.registry,
            driver=self.driver,
            profiles_root=self.profiles_root,
            proxy_pinner=self.proxy_pinner,
            lease_ttl_seconds=self.lease_ttl_seconds,
            tier=str(tier),
            session_name=identity,
            browser_config=self.config,
            prototype_provider=self.prototype_provider,
            egress=self.egress,
            block_hooks=self.extensions.blocks,
        )
        return document


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit  # noqa: PLC0415

    return urlsplit(url).netloc or "default"
