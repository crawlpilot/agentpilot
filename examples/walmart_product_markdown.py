"""Open a session, visit a Walmart product page, print its markdown.

    uv run patchright install chromium          # one-time
    uv run python examples/walmart_product_markdown.py
    uv run python examples/walmart_product_markdown.py <other-walmart-url>

Runs on bundled **Chromium** rather than the default real Chrome. Read
`driver/browser_discovery.py`'s docstring before copying that choice: Chromium
is a bot tell in its own right and Chrome is the default deliberately. The
stealth tier does overwrite the UA and `sec-ch-ua` with a pinned *Chrome* build
(`identity/fingerprint.py`), so the headers agree -- but the runtime does not.
Missing Widevine and the proprietary codecs, a different `navigator.plugins`:
on a site that scores you, Chromium is the harder start. Drop `channel=` to get
Chrome back.

Walmart sits behind Akamai Bot Manager, so this is deliberately not the
`example.com` one-liner from `crawl_to_markdown.py`. Five things matter:

  * **`channel="chromium"`** -- handed straight to Playwright's own registry
    (`browser_discovery.KNOWN_CHANNELS`), so it drives whatever
    `patchright install chromium` put in the browser cache. Also the arm64
    path: `patchright install chrome` publishes no arm64 build.
  * **`tier="stealth"`** -- pins the fingerprint, runs the human warm-up and
    the slow interaction cadence, and turns on body-level block detection.
    `basic` would fetch over plain HTTP first and get walled.
  * **`headful=True`** -- a real window, so you can watch the run and so the
    driver can add the OS-level input path it only has with a display. It is a
    preference, not an assertion: `PatchrightDriver.open` ensures Xvfb where
    that exists and otherwise downgrades to headless with a log line rather
    than failing to launch, so this stays runnable on a display-less box.
    `Browser(headless=...)` is the hard override and wins over this flag.
  * **Warm up on the site root first.** The sensor script runs on
    `walmart.com/`, matures the `_abck` cookie there, and the product page is
    then requested same-site with the root as its referer -- rather than as a
    cold deep link. This is what `run_ephemeral_scrape` does for you on
    protected tiers (`session/ephemeral.py:_build_batch`); in a live session
    you do it yourself, which is the whole reason this example exists.
  * **A persistent `identity`.** The scope handle keeps the profile, the
    pinned proxy and the fingerprint across runs, so the second run reads as a
    returning visitor instead of paying the cold-start warm-up again. It only
    survives the process if `profiles_root` does -- hence the explicit dir.

Everything after that is the ordinary session API: `navigate`, a wait for the
element that proves the page really rendered, then `markdown()`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from crawlpilot.api import Browser, BrowserSession
from crawlpilot.spi.errors import ChallengeDetected, WaitTimeout

PRODUCT_URL = (
    "https://www.walmart.com/ip/Dove-Body-Wash-Strawberry-Cookie-20oz/7843261295"
    "?classType=VARIANT&athbdg=L1300"
)
SITE_ROOT = "https://www.walmart.com/"

PROFILES_ROOT = Path.home() / ".crawlpilot" / "profiles"
"""Outlives the process, so `identity=` below actually means something. Without
it `Browser` mints a temp dir and deletes it on close -- a fresh cold visitor
every run, which on Akamai is the expensive path."""


async def fetch_product_markdown(url: str) -> str:
    PROFILES_ROOT.mkdir(parents=True, exist_ok=True)

    # `channel=` wins over discovery's "real Chrome if you have it" order, so
    # this drives Chromium even on a machine with Chrome installed.
    async with Browser(channel="chromium", profiles_root=PROFILES_ROOT) as browser:
        async with browser.session(
            identity="walmart-shopper",  # opaque scope handle, not a domain
            domain="www.walmart.com",
            tier="stealth",
            headful=True,
            locale="en-US",
            timezone_id="America/New_York",
        ) as page:
            print(f"session {page.session_id} (tier {page.tier})")

            # 1. Warm up on the root. The sensor script gets a page it expects
            #    to be entered cold, and `_abck` gets a chance to validate.
            await page.navigate(SITE_ROOT, wait_until="domcontentloaded")
            await page.wait(1_500)

            # 2. The deep link, now same-site with a referer that fits.
            result = await page.navigate(url, wait_until="domcontentloaded", referer=SITE_ROOT)
            if result.soft_verdict:
                # Rendered, but the classifier is unhappy (too_small /
                # rate_limited / wrong_geo). Worth knowing before you trust
                # the markdown; a hard wall would have raised instead.
                print(f"!! soft verdict: {result.soft_verdict} (weight {result.soft_weight})")

            # 3. Prove the product actually rendered before extracting.
            #    Walmart hydrates the price client-side, so `load` firing is
            #    not evidence that there is anything to read.
            await _wait_for_product(page)

            print(f"title: {await page.get_title()}")
            print(f"url:   {await page.get_url()}")

            # 4. `main_content=True` drops nav, footer and recommendation
            #    rails -- on a retail PDP that is most of the byte count.
            return await page.markdown(main_content=True)


async def _wait_for_product(page: BrowserSession) -> None:
    """Wait for whichever product marker this layout uses.

    Walmart A/B-tests its PDP markup, so a single selector is a flake waiting
    to happen. Any one of these landing means the page is real; none of them
    means we are looking at an interstitial, and the caller should see that
    rather than a confidently-empty markdown string.
    """

    for selector in (
        '[data-testid="price-wrap"]',
        '[itemprop="price"]',
        "#main-title",
        "h1",
    ):
        try:
            await page.wait_for_selector(selector, timeout_ms=8_000)
        except WaitTimeout:
            continue
        return
    raise WaitTimeout("no product marker appeared -- probably an interstitial, not a PDP")


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else PRODUCT_URL
    try:
        markdown = await fetch_product_markdown(url)
    except ChallengeDetected as exc:
        # A hard PRIVACY-scope wall. In a real crawler this is the signal to
        # rotate identity and retry -- `browser.scrape(tier="auto")` has that
        # escalation ladder built in; a live session does not.
        print(f"!! walled: {exc} (verdict {exc.verdict})")
        raise SystemExit(1) from exc

    print(f"\n=== markdown ({len(markdown)} chars)\n")
    print(markdown)


if __name__ == "__main__":
    asyncio.run(main())
