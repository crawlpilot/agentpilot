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
from crawlpilot.tools import ToolRegistry
from crawlpilot.verbs import SessionVerbs

DEFAULT_LEASE_TTL_SECONDS = 300.0


class BrowserSession(SessionVerbs):
    """One open browser context, driven action by action.

    Not constructed directly -- `Browser.session()` yields one and releases it.

    The ~60 verbs live on `SessionVerbs` (`crawlpilot.verbs`), which knows the
    vocabulary and nothing about transport. This class is the local binding:
    `execute` dispatches to the driver, and `tree()` is the one capability that
    exists only in-process.
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

    @property
    def tools(self) -> ToolRegistry:
        """This browser's registry -- built-in verbs plus whatever its
        extensions contributed, which is what makes an extension's namespaced
        tool reachable through `call_tool`."""

        return self._browser.extensions.tools

    # ------------------------------------------------------------- transport

    async def execute(
        self, actions: Sequence[spi_actions.Action], *, page_id: str | None = None
    ) -> ActionResult:
        """Dispatch a batch against the local driver."""

        return await execute_on_session(
            self._session,
            list(actions),
            registry=self._browser.registry,
            driver=self._browser.driver,
            page_id=page_id,
        )

    # --------------------------------------------------------- local-only

    async def tree(self, *, settle: bool = True) -> EnhancedDOMTreeNode | None:
        """The fused DOM tree itself, not the serialized `Snapshot`.

        In-process only, and deliberately not on `SessionVerbs`: the tree is the
        whole fused DOM -- every node, every attribute, parent back-references --
        and it neither survives JSON nor would be worth sending if it did. The
        callers that need it (`agentpilot.agent.observation` diffing snapshots,
        `agentpilot.recipe.*` walking for locators) all run beside the driver.

        Everyone else wants `snapshot()`, which works on any transport.
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
        headful: bool | None = None,
        cdp_url: str | None = None,
    ) -> None:
        """The launch arguments are flat rather than requiring a `BrowserConfig`.

        `executable_path` / `channel` / `headful` / `cdp_url` are what a user
        actually reaches for -- "use my Chrome", "use the browser in that
        container" -- and making them assemble a nested config object to say so
        is the difference between a one-liner and a paragraph. They override the
        matching fields on `config.launch`, so a platform that builds its config
        from the environment still works unchanged.

        `headful` decides visible-window-or-not for **everything this browser
        opens**, `scrape()` included, and it is the same word as
        `session(headful=...)` -- which it overrides. Until 0.2 this argument was
        the inverted `headless=`, and the pair was a genuine trap: `scrape()`
        ignores the session flag entirely and derives headful from the tier rung
        it is on (`session/ephemeral.py`), so an `auto` scrape ran its first
        rung headless no matter what the session asked for, and the only way to
        get a window was the *other*, oppositely-named knob.

        Tri-state, so the default stays honest: `None` means "headful if a
        display exists", `True` asks for a window, `False` forces headless. Even
        `True` degrades rather than fails where there is no display -- see
        `ProcessLauncher.ensure_display`.
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
                        # `LaunchConfig` speaks Playwright's `headless`; the
                        # public argument is the positive one. Both keep `None`
                        # meaning "work it out".
                        ("headless", None if headful is None else not headful),
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
        detect_blocks: bool = False,
        dialogs: spi_actions.DialogPolicy = "auto_dismiss",
    ) -> AsyncIterator[BrowserSession]:
        """Open a session, yield it, always release it.

        `identity` defaults to a fresh throwaway scope whose profile dir is
        deleted on teardown -- a cookie-less first-visit browser, which is what
        a one-shot crawl wants. Pass one to get the opposite: stickiness across
        calls, so repeat visits reuse a profile, a pinned proxy and a
        fingerprint and read as a returning visitor. It is an opaque scope
        handle, never a domain (plan D10).

        `dialogs` decides what happens when the page opens an `alert`/`confirm`/
        `prompt`. The default answers them the way Playwright always has --
        dismissed, silently -- which is right for an unattended crawl that must
        not wedge on one. Pass `"manual"` to have them reported on
        `ActionResult.dialog` and held for `dialog_accept`/`dialog_dismiss`,
        which is what an agent driving the page wants: a `confirm()` is a
        question, and dismissing it unasked turns "delete this" into a no-op
        that still reports success.

        `detect_blocks` decides whether this session can *see* a bot wall. Off,
        the tier's block avoidance still runs (fingerprint, proxy, warm-up) but
        two things silently do not: the warm-up skips its `_abck` wait, and no
        navigation is ever classified -- so a CAPTCHA interstitial is extracted
        and returned as though it were the page you asked for. On, the warm-up
        waits for a validated `_abck` before you read, a soft verdict lands on
        `ActionResult.soft_verdict`, and a hard wall raises `ChallengeDetected`.

        Off by default because an agent run passes through blank pages, SPA
        shells and post-click transitions where `EMPTY`/`TOO_SMALL` are the
        expected state, and because a session has no escalation ladder to answer
        a raised challenge with -- `scrape()` does, which is why it opts in.
        Turn it on for a crawler-shaped session reading a protected page, and
        handle `ChallengeDetected` yourself.
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
            detect_blocks=detect_blocks,
            dialog_policy=dialogs,
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
