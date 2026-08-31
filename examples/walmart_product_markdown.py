"""Read a Walmart product page to markdown, two ways.

    uv run python examples/walmart_product_markdown.py
    uv run python examples/walmart_product_markdown.py <other-walmart-url>

Walmart sits behind Akamai Bot Manager and PerimeterX, so this is deliberately
not the `example.com` one-liner from `crawl_to_markdown.py`. When PerimeterX is
unhappy it serves a *"Robot or human?"* wall -- a hold-the-button challenge --
with an HTTP 200, so nothing about the response says "blocked" except the body.

Two shapes, in the order you should reach for them:

  * `scrape_with_ladder()` -- `browser.scrape(tier="auto")`. Everything that
    answers a wall lives here: the site-root warm-up, the `_abck` wait, block
    classification, and an escalation ladder that retries on a higher tier with
    a fresh identity, proxy and fingerprint (`session/ephemeral.py`).
  * `read_in_session()` -- a live context you can drive. It gets the same
    stealth profile, but it has no ladder: a wall raises and you handle it.

Four things matter on a target like this:

  * **`tier="stealth"`/`"auto"`** -- pins the fingerprint, runs the human
    warm-up and the slow interaction cadence, and turns on body-level block
    detection. `basic` fetches over plain HTTP first and gets walled.
  * **`headful=True`.** A real window, and the OS-level input path the driver
    only has with a display. This is not cosmetic: running headless was what
    served the "Robot or human?" wall on every attempt. A preference, not an
    assertion -- `PatchrightDriver.open` ensures Xvfb where that exists and
    otherwise degrades to headless with a log line rather than failing.
  * **`detect_blocks=True` on the session.** Off (the default, right for agent
    runs) the warm-up skips its `_abck` wait and nothing is ever classified --
    so the wall is extracted and returned as though it were the product page.
  * **A residential exit.** Protected tiers ask for one; without a
    `proxy_pinner` every run leaves from your own IP. Set `CRAWLPILOT_PROXY_URL`
    to wire one -- see `_proxy_pinner()`.

`RetailExtension` contributes Walmart's own block signals (a landed `/blocked`
URL, a `/ip/` page under 300 KB) through the ordinary `BlockMount` seam.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from agentpilot.control.retail_extension import RetailExtension
from crawlpilot.api import Browser, BrowserSession
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy.stores import InMemoryStateStore
from crawlpilot.spi.errors import ChallengeDetected, WaitTimeout
from crawlpilot.spi.proxy import ProxyEndpoint

PRODUCT_URL = (
    "https://www.walmart.com/ip/Dove-Body-Wash-Strawberry-Cookie-20oz/7843261295"
    "?classType=VARIANT&athbdg=L1300"
)
SITE_ROOT = "https://www.walmart.com/"

PROFILES_ROOT = Path.home() / ".crawlpilot" / "profiles"
"""Outlives the process, so `identity=` below actually means something. Without
it `Browser` mints a temp dir and deletes it on close -- a fresh cold visitor
every run, which on a scored site is the expensive path."""


def _proxy_pinner() -> ProxyPinner | None:
    """A residential exit from `CRAWLPILOT_PROXY_URL`, or `None` to go direct.

    `InMemoryStateStore` keeps the identity->endpoint pinning in this process,
    so a single-process crawler needs no Redis. The platform swaps in its
    Redis-backed store and its tenant-aware provider; nothing else changes.
    """

    url = os.environ.get("CRAWLPILOT_PROXY_URL", "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        raise SystemExit(f"CRAWLPILOT_PROXY_URL is not a full proxy URL: {url!r}")
    endpoint = ProxyEndpoint(
        scheme=parts.scheme or "http",
        host=parts.hostname,
        port=parts.port,
        username=parts.username,
        password=parts.password,
        # Protected tiers ask the pool for `residential`; tagging it here is
        # what makes that request match rather than fall through to the
        # whole pool (`policy.providers.StaticProxies.endpoints_for`).
        tier="residential",
        country="US",
    )
    return ProxyPinner(InMemoryStateStore(), [endpoint])


def _browser() -> Browser:
    pinner = _proxy_pinner()
    if pinner is None:
        print(
            "note: no CRAWLPILOT_PROXY_URL -- going out on this machine's own IP.\n"
            "      If you get walled, that is the first thing to change."
        )
    return Browser(
        # Real Chrome by default: `driver/browser_discovery.py` explains why it
        # beats bundled Chromium on a site that fingerprints the browser. Set
        # CRAWLPILOT_BROWSER_CHANNEL=chromium to override (and on arm64 you
        # must -- `patchright install chrome` publishes no arm64 build).
        channel=os.environ.get("CRAWLPILOT_BROWSER_CHANNEL") or None,
        profiles_root=PROFILES_ROOT,
        extensions=[RetailExtension()],
        proxy_pinner=pinner,
    )


async def scrape_with_ladder(url: str) -> str:
    """One-shot, with the escalation ladder. The path to reach for first.

    `auto` starts on `stealth` and climbs to `enhanced` on a hard wall, minting
    a fresh identity -- and so a new proxy and fingerprint -- for each attempt.
    The `enhanced` rung also requests headful of its own accord.
    """

    PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    async with _browser() as browser:
        document = await browser.scrape(url, tier="auto", formats=("markdown",))
        print(f"tier used: {document.metadata.tier_used}  status: {document.metadata.status_code}")
        print(f"title:     {document.metadata.title}")
        return document.markdown or ""


async def read_in_session(url: str) -> str:
    """A live context. No ladder -- a wall raises and the caller decides."""

    PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    async with _browser() as browser:
        async with browser.session(
            identity="walmart-shopper",  # opaque scope handle, not a domain
            domain="www.walmart.com",
            tier="stealth",
            headful=True,
            detect_blocks=True,
            locale="en-US",
            timezone_id="America/New_York",
        ) as page:
            print(f"session {page.session_id} (tier {page.tier})")

            # 1. Warm up on the root. The sensor script gets a page it expects
            #    to be entered cold, and -- because `detect_blocks=True` -- the
            #    warm-up waits for `_abck` to leave its `~-1~` unsolved state
            #    before we go anywhere. That wait is the whole point of warming
            #    up here, and it is exactly what a default session skips.
            await page.navigate(SITE_ROOT, wait_until="domcontentloaded")

            # 2. The deep link, now same-site with a referer that fits.
            result = await page.navigate(url, wait_until="domcontentloaded", referer=SITE_ROOT)
            if result.soft_verdict:
                # Rendered, but the classifier is unhappy (too_small /
                # rate_limited / wrong_geo). A hard wall would have raised.
                print(f"!! soft verdict: {result.soft_verdict} (weight {result.soft_weight})")

            # 3. Prove the product rendered before extracting. Walmart hydrates
            #    the price client-side, so `load` firing is not evidence that
            #    there is anything to read.
            await _wait_for_product(page)

            print(f"title: {await page.get_title()}")
            print(f"url:   {await page.get_url()}")

            # 4. `main_content=True` drops nav, footer and recommendation
            #    rails -- on a retail PDP that is most of the byte count.
            return await page.markdown(main_content=True)


async def _wait_for_product(page: BrowserSession) -> None:
    """Wait for whichever product marker this layout uses.

    Walmart A/B-tests its PDP markup, so a single selector is a flake waiting
    to happen. Deliberately no bare `h1` fallback: the "Robot or human?" wall
    has an `h1` too, so that fallback turned this guard into a rubber stamp
    that let the wall through to `markdown()`.
    """

    for selector in ('[data-testid="price-wrap"]', '[itemprop="price"]', "#main-title"):
        try:
            await page.wait_for_selector(selector, timeout_ms=8_000)
        except WaitTimeout:
            continue
        return
    # Nothing product-shaped. Say what the page actually is rather than guess:
    # with `detect_blocks=True` a hard wall would already have raised, so this
    # is the softer "not what we asked for" case.
    raise WaitTimeout(f"no product marker on {await page.get_url()} -- not a product page")


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else PRODUCT_URL
    try:
        markdown = await scrape_with_ladder(url)
    except ChallengeDetected as exc:
        # Every rung of the ladder hit a wall. In a real crawler this is where
        # you rotate egress rather than retry harder.
        print(f"!! walled after the full ladder: {exc} (verdict {exc.verdict})")
        raise SystemExit(1) from exc

    print(f"\n=== markdown ({len(markdown)} chars)\n")
    print(markdown[:2000].rstrip())


if __name__ == "__main__":
    asyncio.run(main())
