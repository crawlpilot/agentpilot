"""Crawl a few pages to markdown -- no gateway, no HTTP API, no config files.

    uv run python examples/crawl_to_markdown.py https://example.com

This is the shape the browserpilot extraction exists to make possible: a
crawler pipeline importing a library and driving a browser directly. Before the
facade, the equivalent meant constructing a driver, a registry, a proxy pinner,
a vault, a profiles root and lease TTLs by hand -- assembly that only the HTTP
gateway's 500-line composition root knew how to do.

Two shapes, both shown below:

  * `browser.scrape(url)` -- one-shot. Mints a throwaway identity, runs one
    batch, tears the context down. Right for a list of independent URLs.
  * `browser.session()` -- a live context across many calls. Right when you
    need to interact (fill, click, follow) rather than just read.
"""

from __future__ import annotations

import asyncio
import sys

from crawlpilot.api import Browser

DEFAULT_URLS = [
    "https://example.com",
    "https://www.iana.org/domains/reserved",
    "https://httpbin.org/html",
]


async def scrape_many(urls: list[str]) -> None:
    """One-shot per URL. Each page gets a fresh, cookie-less identity."""

    async with Browser() as browser:
        for url in urls:
            try:
                document = await browser.scrape(url, tier="basic")
            except Exception as exc:  # noqa: BLE001 -- a demo should not die on one bad page
                print(f"\n=== {url}\n!! {type(exc).__name__}: {exc}")
                continue
            markdown = document.markdown or ""
            print(f"\n=== {url}  ({len(markdown)} chars)")
            print(markdown[:400].rstrip())


async def read_one_interactively(url: str) -> None:
    """A live session: navigate, then read. The same object an agent drives."""

    async with Browser() as browser:
        # `identity=` would make this a returning visitor across runs; omitted,
        # so this is a first-visit browser whose profile is deleted on teardown.
        async with browser.session() as page:
            await page.navigate(url)
            print(f"\n=== interactive: {url}")
            print((await page.markdown())[:400].rstrip())


async def main() -> None:
    urls = sys.argv[1:] or DEFAULT_URLS
    await scrape_many(urls)
    await read_one_interactively(urls[0])


if __name__ == "__main__":
    asyncio.run(main())
