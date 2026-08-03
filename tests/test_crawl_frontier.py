"""`agentpilot.crawl.frontier.expand_frontier`."""

from __future__ import annotations

from agentpilot.crawl.frontier import expand_frontier
from agentpilot.spi.crawl import CrawlOptions

SEED = "https://example.com/blog/"
PAGE_HTML = """<html><body>
<a href="/blog/post-2">Post 2</a>
<a href="/about">About</a>
<a href="https://other.com/x">External</a>
</body></html>"""


def test_extracts_and_filters_links_from_the_page() -> None:
    options = CrawlOptions(url=SEED)
    urls = expand_frontier(
        html=PAGE_HTML, page_url=SEED, depth=0, seed_url=SEED, options=options, robots_parser=None
    )
    assert urls == ["https://example.com/blog/post-2"]  # /about backward, other.com external


def test_returns_empty_once_max_discovery_depth_reached() -> None:
    options = CrawlOptions(url=SEED, max_discovery_depth=1)
    urls = expand_frontier(
        html=PAGE_HTML, page_url=SEED, depth=1, seed_url=SEED, options=options, robots_parser=None
    )
    assert urls == []


def test_no_depth_limit_when_max_discovery_depth_is_none() -> None:
    options = CrawlOptions(url=SEED, max_discovery_depth=None)
    urls = expand_frontier(
        html=PAGE_HTML, page_url=SEED, depth=999, seed_url=SEED, options=options, robots_parser=None
    )
    assert urls == ["https://example.com/blog/post-2"]


def test_dedups_repeated_links_on_the_same_page() -> None:
    html = '<a href="/blog/post-2">A</a><a href="/blog/post-2">A again</a>'
    options = CrawlOptions(url=SEED)
    urls = expand_frontier(
        html=html, page_url=SEED, depth=0, seed_url=SEED, options=options, robots_parser=None
    )
    assert urls == ["https://example.com/blog/post-2"]
