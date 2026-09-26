"""Site-discovery request shapes shared by `/v1/crawl`, `/v1/batch/scrape`,
and `/v1/map` -- adapted from Firecrawl's `crawlerOptions`/`MapRequest`
(`apps/api/src/controllers/v2/types.ts`), field names kept 1:1 where the
concept carries over so this reads as "the same crawl options, this
platform's engine" rather than a fresh vocabulary.

These options drive `agentpilot.crawl`'s own discovery/filtering modules, which
is why they live here: site discovery is this platform's concern, and the
browser library never referenced them. `ScrapeOptions` -- the per-page half,
which crawlpilot genuinely does own and act on -- stays in `crawlpilot.spi`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from crawlpilot.spi.scrape import ScrapeOptions

SitemapMode = Literal["skip", "include", "only"]

CacheMode = Literal["enabled", "bypass", "read_only", "write_only", "disabled"]
"""Re-exported from `agentpilot.jobs.cache` rather than imported, to keep this
module a leaf: `crawl.types` is loaded by the gateway's request mapping and by
`jobs.options_codec`, and neither should acquire a dependency on the cache
implementation just to name a mode. `jobs.cache.CacheMode` is the same
`Literal`, and a test asserts the two stay identical."""


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
    """Minimum gap between requests to one host. Honoured by `jobs.limiter`,
    which takes the larger of this and the host's robots.txt `Crawl-delay` --
    a caller's smaller number cannot override what a host published."""
    max_concurrency: int = 10
    cache_mode: CacheMode = "enabled"
    max_age_ms: int | None = None
    """How old a cached page may be and still be served, in milliseconds.
    `None` takes the deployment's default (`jobs.cache.DEFAULT_MAX_AGE_MS`).
    Job-level rather than per-page, alongside `delay_ms`: freshness is a
    property of why the caller is crawling, not of any one URL in it."""
    scrape_options: ScrapeOptions = field(default_factory=ScrapeOptions)


@dataclass
class BatchScrapeOptions:
    urls: tuple[str, ...]
    cache_mode: CacheMode = "enabled"
    max_age_ms: int | None = None
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
