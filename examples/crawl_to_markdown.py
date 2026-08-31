"""Crawl a few pages to markdown -- no gateway, no HTTP API, no config files.

    uv run python examples/crawl_to_markdown.py https://example.com

This is the async shape. `quickstart.py` is the same thing without `await`;
`AsyncCrawlpilot` exists for callers who already have an event loop -- a
FastAPI handler, an agent runtime, anything that would otherwise block it.

Two shapes, both shown below:

  * `cp.scrape(url)` / `cp.batch_scrape(urls)` -- one-shot. Mints a throwaway
    identity, runs one batch, tears the context down. Right for a list of
    independent URLs.
  * `cp.session()` -- a live context across many calls. Right when you need to
    interact (fill, click, follow) rather than just read. See
    `snapshot_and_click.py` for that in earnest.
"""

from __future__ import annotations

import asyncio
import sys

from crawlpilot import AsyncCrawlpilot

DEFAULT_URLS = [
    "https://example.com",
    "https://www.iana.org/domains/reserved",
    "https://httpbin.org/html",
]


async def scrape_many(urls: list[str]) -> None:
    """One-shot per URL, each with a fresh cookie-less identity.

    `batch_scrape` returns one `Document` per URL in the order given; a page
    that failed carries `error` instead of `markdown`, so one bad URL does not
    take the run down with it.
    """

    async with AsyncCrawlpilot() as cp:
        for document in await cp.batch_scrape(urls):
            if document.error:
                print(f"\n=== {document.url}\n!! {document.error}")
                continue
            markdown = document.markdown or ""
            print(f"\n=== {document.url}  ({len(markdown)} chars)")
            print(markdown[:400].rstrip())


async def read_one_interactively(url: str) -> None:
    """A live session: navigate, then read. The same object an agent drives."""

    async with AsyncCrawlpilot() as cp:
        # `identity=` would make this a returning visitor across runs; omitted,
        # so this is a first-visit browser whose profile is deleted on teardown.
        async with cp.session() as page:
            await page.navigate(url)
            print(f"\n=== interactive: {url}")
            print(f"title: {await page.get_title()}")
            print((await page.markdown())[:400].rstrip())


async def main() -> None:
    urls = sys.argv[1:] or DEFAULT_URLS
    await scrape_many(urls)
    await read_one_interactively(urls[0])


if __name__ == "__main__":
    asyncio.run(main())
