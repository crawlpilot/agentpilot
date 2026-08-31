"""The one-call client: defaults, proxy parsing, batch error isolation, sync.

No browser -- a recording driver stands in, so these run in milliseconds and in
a venv with no Chrome.
"""

from __future__ import annotations

from typing import Any

import pytest

from crawlpilot import AsyncCrawlpilot, Crawlpilot, proxy_endpoint
from crawlpilot.client import DEFAULT_TIER

from .test_api_facade import RecordingDriver

# --------------------------------------------------------------- proxy parsing


def test_proxy_endpoint_parses_a_url() -> None:
    endpoint = proxy_endpoint("http://user:secret@gw.example:8080", country="US")

    assert (endpoint.scheme, endpoint.host, endpoint.port) == ("http", "gw.example", 8080)
    assert (endpoint.username, endpoint.password) == ("user", "secret")
    assert endpoint.country == "US"


def test_proxy_endpoint_defaults_to_the_residential_tier() -> None:
    """Protected tiers ask the pool for `residential`. An endpoint with no tier
    is only reachable through `StaticProxies`' fall-through, so the default tag
    is what makes the request actually match."""

    assert proxy_endpoint("http://gw.example:8080").tier == "residential"


@pytest.mark.parametrize("bad", ["gw.example", "http://gw.example", "", "not a url"])
def test_proxy_endpoint_rejects_anything_that_is_not_host_and_port(bad: str) -> None:
    with pytest.raises(ValueError, match="full URL"):
        proxy_endpoint(bad)


def test_a_proxy_url_becomes_a_wired_pinner(tmp_path: Any) -> None:
    """The point of the argument: a URL in, a `ProxyPinner` with its store and
    endpoint already assembled out. That was three imports and a `StateStore`
    the caller had never heard of."""

    client = AsyncCrawlpilot(
        proxy="http://u:p@gw.example:8080",
        driver=RecordingDriver(),
        profiles_root=tmp_path,
    )
    pinner = client.browser.proxy_pinner

    assert pinner is not None
    assert [e.host for e in pinner.pool] == ["gw.example"]


def test_no_proxy_means_no_pinner(tmp_path: Any) -> None:
    client = AsyncCrawlpilot(driver=RecordingDriver(), profiles_root=tmp_path)
    assert client.browser.proxy_pinner is None


# -------------------------------------------------------------------- defaults


def test_detect_blocks_defaults_on(tmp_path: Any) -> None:
    """Opposite of `Browser.session()`, deliberately.

    The low layer defaults to what an agent run needs, where thin and blank
    pages are ordinary. Someone who called `Crawlpilot()` to fetch a page wants
    to be told when what came back is a CAPTCHA rather than the page.
    """

    assert AsyncCrawlpilot(driver=RecordingDriver(), profiles_root=tmp_path)._detect_blocks is True
    assert (
        AsyncCrawlpilot(
            detect_blocks=False, driver=RecordingDriver(), profiles_root=tmp_path
        )._detect_blocks
        is False
    )


def test_the_default_tier_escalates() -> None:
    """`auto` starts cheap and climbs only when something blocks -- both the
    fastest and the most robust default, so it is not a decision the caller has
    to make."""

    assert DEFAULT_TIER == "auto"


# ----------------------------------------------------------------------- batch


class FlakyDriver(RecordingDriver):
    """Fails on any URL containing `boom`, the way a dead host really does."""

    async def execute(self, ctx: Any, actions: list[Any], page_id: Any = None) -> Any:
        for action in actions:
            if getattr(action, "url", None) and "boom" in action.url:
                # Not a `DriverError`: a dead URL surfaces as Patchright's own
                # exception type, which is exactly what a narrow `except` misses.
                raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        return await super().execute(ctx, actions, page_id)


async def test_batch_scrape_isolates_a_failing_url(tmp_path: Any) -> None:
    """One bad URL must not discard the good results around it."""

    async with AsyncCrawlpilot(driver=FlakyDriver(), profiles_root=tmp_path) as cp:
        docs = await cp.batch_scrape(
            ["https://ok.test/a", "https://boom.test/b", "https://ok.test/c"]
        )

    assert [d.url for d in docs] == [
        "https://ok.test/a",
        "https://boom.test/b",
        "https://ok.test/c",
    ], "results come back in the order given"
    assert docs[0].error is None
    assert docs[1].error is not None and "ERR_NAME_NOT_RESOLVED" in docs[1].error
    assert docs[2].error is None, "a later URL still runs after an earlier failure"


# ------------------------------------------------------------------ sync shape


def test_sync_client_needs_no_event_loop(tmp_path: Any) -> None:
    """The headline of the facade: no `asyncio.run`, no `await`."""

    with Crawlpilot(driver=RecordingDriver(), profiles_root=tmp_path) as cp:
        doc = cp.scrape("https://example.test/")
        assert doc.markdown

        with cp.session() as page:
            page.navigate("https://example.test/")
            # `SyncSession.__getattr__` forwards to the client's loop, so this
            # needs no per-method wrapper and cannot drift from `BrowserSession`.
            assert isinstance(page.session_id, str)


def test_sync_and_async_agree(tmp_path: Any) -> None:
    """`Crawlpilot` is `AsyncCrawlpilot` with the `await` taken off -- the async
    class owns the behaviour, so the two cannot answer differently."""

    import asyncio

    with Crawlpilot(driver=RecordingDriver(), profiles_root=tmp_path) as cp:
        sync_doc = cp.scrape("https://example.test/")

    async def run() -> Any:
        async with AsyncCrawlpilot(driver=RecordingDriver(), profiles_root=tmp_path) as cp:
            return await cp.scrape("https://example.test/")

    assert sync_doc.markdown == asyncio.run(run()).markdown
