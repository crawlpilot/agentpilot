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

from agentpilot.crawl.sources import DEFAULT_SOURCES, SourceName
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
    query: str | None = None
    """What the crawl is looking for, used to prioritise the frontier
    (`crawl.scorers.relevance`). Not a filter -- nothing is excluded for failing
    to match; matching URLs are simply fetched first, which is what decides
    *which* `limit` pages a bounded crawl of a large site comes back with."""
    score_urls: bool = True
    """Order the frontier by `crawl.scorers.composite` instead of the order links
    appeared in the DOM. On by default: DOM order means a `limit` of 500 against a
    50,000-page site returns that site's navigation and footer, which is nobody's
    intent. Set `False` for the previous first-seen behaviour."""
    confidence_threshold: float | None = None
    """Stop the crawl once it has learned enough about `query`, rather than when it
    exhausts `limit` (`crawl.adaptive`). `0.7` is a reasonable starting point.

    Requires `query` -- there is nothing to be confident *about* otherwise, and a
    threshold without one is ignored rather than guessed at. `limit` stays in force
    as the ceiling: adaptive stopping only ever ends a crawl earlier."""
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
    sources: tuple[SourceName, ...] = DEFAULT_SOURCES
    """Which discovery sources to run (`crawl.sources`). The default four only
    ever talk to the target site. `cc`/`wayback`/`crt` send the target domain to a
    third-party index and `probe` sends speculative requests to the site -- both
    are opt-in for that reason, not because they are less useful."""
    source_timeout: float = 30.0
    """Per-source deadline. One slow source cannot set the latency of the whole
    request; it just contributes nothing and is reported."""
    max_subdomains: int = 10
    """How many discovered subdomains to scan for their own URLs.

    `crt`/`wayback` report *hosts*; without this they only widened the URL set
    indirectly, through URLs those sources already happened to hold. Scanning each
    confirmed host's own sitemap and homepage is what turns "map example.com" into
    something that actually covers the domain rather than just `www`.

    Bounded because it is a fan-out: each host costs a DNS lookup and two requests.
    `0` disables the pass. Only runs when `include_subdomains` is set and a source
    that reports hosts was enabled."""
    detect_soft_404: bool = True
    """Fingerprint the site's not-found page and drop results that match it. Costs
    one request per origin and is what keeps an SPA from reporting every probed
    path as a real page."""
    include_metadata: bool = False
    """Fetch each result's `<title>` and description (`crawl.head`). Off by
    default because it costs one bounded request per URL -- worth it for a
    hundred-link map a human will read, wasteful for a hundred-thousand-link one
    feeding a pipeline."""
    filter_nonsense: bool = True
    """Drop site machinery -- assets, webpack chunks, `robots.txt`, archived
    sitemaps (`crawl.nonsense`). Mostly matters once `wayback` or `cc` are on;
    they return a great deal of it."""
    timeout_ms: int | None = None


@dataclass
class MapLink:
    url: str
    title: str | None = None
    description: str | None = None
    score: float | None = None
    """The ranking score (`crawl.scorers.composite`) when `search` was given, so a
    caller can see *why* the order is what it is rather than having to trust it.
    `None` when no query was supplied and the set is unranked."""
