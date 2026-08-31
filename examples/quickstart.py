"""The smallest thing that works.

    uv run python examples/quickstart.py
    uv run python examples/quickstart.py https://example.com https://iana.org

No event loop, no configuration, no browser assembly -- `Crawlpilot()` picks a
browser, a stealth tier that escalates on a wall, and block detection, because
none of those are decisions a first scrape has an opinion about.

`AsyncCrawlpilot` is the same object with `await` in front of each call; see
`crawl_to_markdown.py` for that shape, and `snapshot_and_click.py` for driving
a live page.
"""

from __future__ import annotations

import sys

from crawlpilot import Crawlpilot

DEFAULT_URLS = ["https://example.com", "https://www.iana.org/domains/reserved"]


def main() -> None:
    urls = sys.argv[1:] or DEFAULT_URLS

    with Crawlpilot() as cp:
        # One page.
        doc = cp.scrape(urls[0])
        print(f"=== {doc.url}  ({len(doc.markdown or '')} chars)")
        print((doc.markdown or "")[:300].rstrip())

        if len(urls) > 1:
            # Several pages. A URL that fails comes back carrying `error`
            # rather than taking the whole batch down with it.
            print("\n=== batch")
            for result in cp.batch_scrape(urls):
                status = result.error or f"{len(result.markdown or '')} chars"
                print(f"  {result.url:50} {status}")


if __name__ == "__main__":
    main()
