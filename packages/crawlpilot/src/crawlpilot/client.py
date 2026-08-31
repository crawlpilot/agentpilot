"""The one-call entry point: `Crawlpilot` and `AsyncCrawlpilot`.

`Browser` (in `crawlpilot.api`) is the assembly layer -- every part injectable,
which is what the platform needs and what a crawler does not. Getting one
protected page to markdown through it took five constructor arguments, imports
from six modules, and two knobs whose interaction you had to know:

    async with Browser(
        channel=..., headful=True, profiles_root=...,
        extensions=[RetailExtension()],
        proxy_pinner=ProxyPinner(InMemoryStateStore(), [ProxyEndpoint(...)]),
    ) as browser:
        doc = await browser.scrape(url, tier="auto")

Every comparable library is one call -- Firecrawl's `client.scrape(url)`,
BrightData's and Oxylabs' equivalents -- and none of that assembly is a decision
a first-time caller has an opinion about. So this module is the same platform
with the decisions already made:

    with Crawlpilot() as cp:
        print(cp.scrape("https://example.com").markdown)

`Browser` stays exactly as it was, and `session()` here hands you one, so
dropping to the lower layer is one call rather than a re-assembly.

**Sync is the headline.** `AsyncCrawlpilot` holds all the logic; `Crawlpilot`
runs it on a private event loop in a worker thread. Not `asyncio.run` per call
-- that would tear down the browser between calls, which is the expensive part.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from crawlpilot._sync import LoopThread, SyncProxy
from crawlpilot.api import Browser, BrowserSession
from crawlpilot.extensions import Extension
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy.stores import InMemoryStateStore
from crawlpilot.spi.proxy import ProxyEndpoint
from crawlpilot.spi.scrape import Document, DocumentMetadata
from crawlpilot.tiers import Tier

T = TypeVar("T")

DEFAULT_TIER = "auto"
"""Start cheap, escalate on a wall. The `basic` rung fetches over plain HTTP
before spending a browser, and `auto` climbs to `stealth` then `enhanced` only
when something actually blocks -- so this is both the fastest and the most
robust default, which is why it is not a decision the caller has to make."""


def proxy_endpoint(
    url: str, *, tier: str = "residential", country: str | None = None
) -> ProxyEndpoint:
    """A `ProxyEndpoint` from an ordinary proxy URL.

    Wiring a proxy was three imports from three modules and a `StateStore` the
    caller had never heard of. It is a URL; this takes a URL.
    """

    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        raise ValueError(
            f"proxy must be a full URL with host and port, e.g. "
            f"'http://user:pass@host:8080' (got {url!r})"
        )
    return ProxyEndpoint(
        scheme=parts.scheme or "http",
        host=parts.hostname,
        port=parts.port,
        username=parts.username,
        password=parts.password,
        # Protected tiers ask the pool for `residential`; without the tag the
        # request falls through to the whole pool rather than matching.
        tier=tier,
        country=country,
    )


class AsyncCrawlpilot:
    """The async client. `Crawlpilot` is this, without the `await`."""

    def __init__(
        self,
        *,
        proxy: str | Sequence[str] | None = None,
        proxy_country: str | None = None,
        headful: bool | None = None,
        channel: str | None = None,
        executable_path: str | Path | None = None,
        cdp_url: str | None = None,
        cdp_headers: Mapping[str, str] | None = None,
        profiles_root: Path | None = None,
        extensions: Sequence[Extension] = (),
        detect_blocks: bool = True,
        **browser_kwargs: Any,
    ) -> None:
        """Every argument optional, and every default the one this session's
        debugging showed you actually want.

        `proxy` takes a URL (or several) rather than a `ProxyPinner` built from
        a `StateStore` and a list of `ProxyEndpoint`s. `detect_blocks` defaults
        **on** here though it is off on `Browser.session()`: the low layer
        defaults to what an agent run needs, where thin and blank pages are
        ordinary; a caller who came here to fetch a page wants to be told when
        what came back is a CAPTCHA rather than the page.

        `extensions` is how site knowledge arrives -- `RetailExtension` from
        `agentpilot.control` is the reference one. It is not bundled, and
        deliberately: an import-linter contract keeps site-specific policy out
        of the browser layer.
        """

        urls = [proxy] if isinstance(proxy, str) else list(proxy or ())
        pinner = (
            ProxyPinner(
                InMemoryStateStore(),
                [proxy_endpoint(u, country=proxy_country) for u in urls],
            )
            if urls
            else None
        )
        self._detect_blocks = detect_blocks
        self.browser = Browser(
            proxy_pinner=pinner,
            headful=headful,
            channel=channel,
            executable_path=executable_path,
            cdp_url=cdp_url,
            cdp_headers=cdp_headers,
            profiles_root=profiles_root,
            extensions=extensions,
            **browser_kwargs,
        )

    async def __aenter__(self) -> AsyncCrawlpilot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.browser.close()

    # ------------------------------------------------------------------ scrape

    async def scrape(
        self,
        url: str,
        *,
        formats: Sequence[str] = ("markdown",),
        tier: Tier | str = DEFAULT_TIER,
        **options: Any,
    ) -> Document:
        """One page, one call. The escalation ladder is on by default."""

        return await self.browser.scrape(url, formats=formats, tier=tier, **options)

    async def batch_scrape(
        self,
        urls: Sequence[str],
        *,
        formats: Sequence[str] = ("markdown",),
        tier: Tier | str = DEFAULT_TIER,
        **options: Any,
    ) -> list[Document]:
        """Several pages, one call, one `Document` each -- in the order given.

        A page that fails comes back as a `Document` carrying `error` rather
        than raising, because the alternative is a fifty-URL run that throws
        away forty-nine good results over one bad URL. Check `doc.error`, or
        filter on `doc.markdown`.
        """

        out: list[Document] = []
        for url in urls:
            try:
                out.append(await self.scrape(url, formats=formats, tier=tier, **options))
            except Exception as exc:  # noqa: BLE001
                # Deliberately everything. Narrowing this to crawlpilot's own
                # `DriverError` family misses the ones that actually happen: a
                # dead URL surfaces as Patchright's `Error` ("net::
                # ERR_FILE_NOT_FOUND"), and a batch that dies on one bad URL
                # while discarding forty-nine good results is precisely what
                # this method exists to prevent. The exception is not swallowed
                # -- it is what `Document.error` carries.
                out.append(_failed_document(url, exc))
        return out

    # ----------------------------------------------------------------- session

    @asynccontextmanager
    async def session(self, **kwargs: Any) -> AsyncIterator[BrowserSession]:
        """A live `BrowserSession` -- the lower layer, already assembled.

        `detect_blocks` is inherited from the client unless overridden here.
        """

        kwargs.setdefault("detect_blocks", self._detect_blocks)
        async with self.browser.session(**kwargs) as page:
            yield page


class Crawlpilot:
    """The sync client. Same methods as `AsyncCrawlpilot`, without the `await`.

        with Crawlpilot() as cp:
            print(cp.scrape("https://example.com").markdown)

    One event loop on one worker thread for the life of the client, rather than
    `asyncio.run` per call: the browser is the expensive thing here, and a loop
    per call would tear it down and relaunch it between `scrape`s.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._loop = LoopThread(name="crawlpilot")
        self._async = AsyncCrawlpilot(**kwargs)

    def _call(self, coro: Any) -> Any:
        return self._loop.call(coro)

    def __enter__(self) -> Crawlpilot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._loop.closed:
            return
        self._call(self._async.close())
        self._loop.close()

    # The async client owns the behaviour; these only cross the thread boundary.

    def scrape(self, url: str, **kwargs: Any) -> Document:
        return self._call(self._async.scrape(url, **kwargs))  # type: ignore[no-any-return]

    def batch_scrape(self, urls: Sequence[str], **kwargs: Any) -> list[Document]:
        return self._call(self._async.batch_scrape(urls, **kwargs))  # type: ignore[no-any-return]

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[SyncSession]:
        """A live session, driven synchronously.

        The context manager is entered and exited on the client's loop, so the
        browser context lives exactly as long as the `with` block.
        """

        kwargs.setdefault("detect_blocks", self._async._detect_blocks)
        ctx = self._async.browser.session(**kwargs)
        page = self._call(ctx.__aenter__())
        try:
            yield SyncSession(page, self._call)
        finally:
            self._call(ctx.__aexit__(None, None, None))


class SyncSession(SyncProxy):
    """A `BrowserSession` with the `await` taken off.

    The forwarding lives in `_sync.SyncProxy`, which a remote client's sync half
    reuses rather than reimplementing -- see that module. This subclass exists to
    keep the name a caller sees (`crawlpilot.SyncSession`) tied to what it wraps.
    """

    def __init__(self, page: BrowserSession, call: Any) -> None:
        super().__init__(page, call)


def _failed_document(url: str, exc: Exception) -> Document:
    """A `Document` that records why this URL produced nothing."""

    return Document(
        document_id="",
        url=url,
        error=f"{type(exc).__name__}: {exc}",
        metadata=DocumentMetadata(
            title=None,
            status_code=None,
            tier_used="",
            node_id="",
            duration_ms=0.0,
            source_url=url,
        ),
    )
