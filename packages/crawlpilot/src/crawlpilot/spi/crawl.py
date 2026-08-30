"""Site-discovery request shapes shared by `/v1/crawl`, `/v1/batch/scrape`,
and `/v1/map` -- adapted from Firecrawl's `crawlerOptions`/`MapRequest`
(`apps/api/src/controllers/v2/types.ts`), field names kept 1:1 where the
concept carries over so this reads as "the same crawl options, this
platform's engine" rather than a fresh vocabulary.

`agentpilot.crawl` (the discovery/filtering module these options drive) is a
pure, driver-free leaf module -- see that package's own docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agentpilot.spi.scrape import ScrapeOptions

SitemapMode = Literal["skip", "include", "only"]


@dataclass
class CrawlOptions:
    url: str
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    max_discovery_depth: int | None = None
    limit: int = 10_000
    allow_external_links: bool = False
    allow_subdomains: bool = False
    allow_backward_crawling: bool = False
    ignore_robots_txt: bool = False
    sitemap: SitemapMode = "include"
    deduplicate_similar_urls: bool = True
    ignore_query_parameters: bool = False
    delay_ms: int | None = None
    max_concurrency: int = 10
    scrape_options: ScrapeOptions = field(default_factory=ScrapeOptions)


@dataclass
class BatchScrapeOptions:
    urls: tuple[str, ...]
    scrape_options: ScrapeOptions = field(default_factory=ScrapeOptions)


@dataclass
class MapOptions:
    url: str
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    sitemap: SitemapMode = "include"
    include_subdomains: bool = False
    ignore_query_parameters: bool = False
    limit: int = 100_000
    search: str | None = None
    """Relevance query -- when set, discovered links are re-ranked by cosine
    similarity of their URL string against this query (port of Firecrawl's
    `performCosineSimilarity`). Discovery itself is unchanged; this only
    reorders the result set."""
    allow_external_links: bool = False
    filter_by_path: bool = True
    """When the seed URL has a non-trivial path (not `/`), keep only links
    whose path starts with it -- mirrors Firecrawl map-utils' `filterByPath`,
    so mapping `example.com/blog` returns blog URLs, not the whole site.
    Ignored when `allow_external_links` is set."""
    max_discovery_depth: int = 2
    """Depth bound for the recursive-crawl fallback (the driver-free
    substitute for Firecrawl's search-engine/index sources): 0 = seed page
    only, 1 = seed + its links' pages, etc."""
    max_crawl_pages: int = 500
    """Hard cap on pages the recursive fallback fetches, independent of
    `limit` (which caps returned URLs) -- guards against runaway fan-out."""
    timeout_ms: int | None = None


@dataclass
class MapLink:
    url: str
    title: str | None = None
    description: str | None = None
