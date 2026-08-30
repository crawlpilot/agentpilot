"""Given `CrawlOptions`/`MapOptions`, produces the initial candidate URL
batch: sitemap-sourced links (unless `sitemap == "skip"`) plus a shallow
guarded fetch of the seed URL's own HTML for its `<a href>` links (unless
`sitemap == "only"`), normalized and filtered. Shared by `/v1/crawl` (seeds
`crawl_tasks`, P4) and `/v1/map` (returns links directly, synchronous,
never touching the job queue) -- structural port of Firecrawl's
`WebCrawler.tryGetSitemap` orchestration.

This is a *shallow* discovery pass, not a full crawl: it fetches the seed
page itself (never rendered -- plain guarded HTTP, no browser) plus the
sitemap, but does not recursively follow the links it finds. Recursive,
depth-tracked frontier expansion as pages actually get scraped is
`agentpilot.jobs`'s crawl-worker loop's job (P4, `frontier.py`), which reuses
`filters`/`dedup` from this package directly rather than going through this
module again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from agentpilot.crawl import dedup, filters, link_extractor, rank, robots, sitemap
from agentpilot.egress.httpx_guard import guarded_get
from agentpilot.spi.crawl import CrawlOptions, MapLink, MapOptions
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import EgressBlocked

# Bounded concurrency for the recursive-crawl fallback's per-level page
# fetches -- matches `CrawlOptions.max_concurrency`'s default so map's
# driver-free discovery is no more aggressive than the crawl worker itself.
_CRAWL_FETCH_CONCURRENCY = 10


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _fetch_page_html(url: str, policy: EgressPolicy) -> str | None:
    """`None` on any fetch failure or a non-HTML/4xx+ response -- same
    fail-open contract as `sitemap.fetch_urls()`/`robots.fetch()`, so a dead
    or non-HTML page just contributes no links rather than aborting."""

    try:
        resp = await guarded_get(url, policy, timeout=15.0, follow_redirects=True)
    except (EgressBlocked, httpx.HTTPError):
        return None
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
        return None
    return resp.text


async def _fetch_seed_page_links(url: str, policy: EgressPolicy) -> list[str]:
    html = await _fetch_page_html(url, policy)
    return link_extractor.extract_links(html, url) if html is not None else []


async def _gather_sitemap_urls(url: str, policy: EgressPolicy) -> list[str]:
    """The default `/sitemap.xml` plus every sitemap declared in robots.txt --
    mirrors Firecrawl seeding sitemap discovery from `robots.getSitemaps()`,
    which the prior port missed (many sites declare their sitemap only in
    robots.txt). Robots fetch failure is fail-open (no extra sitemaps), same
    as everywhere else in this package."""

    origin = _origin(url)
    sitemap_urls: set[str] = {sitemap.default_sitemap_url(origin)}
    robots_parser = await robots.fetch(origin, policy)
    if robots_parser is not None:
        for declared in robots_parser.site_maps() or []:
            sitemap_urls.add(declared)

    out: list[str] = []
    for sitemap_url in sitemap_urls:
        out.extend(await sitemap.fetch_urls(sitemap_url, policy))
    return out


async def _discover_candidates(url: str, sitemap_mode: str, policy: EgressPolicy) -> list[str]:
    candidates: list[str] = []
    if sitemap_mode != "skip":
        candidates.extend(
            await sitemap.fetch_urls(sitemap.default_sitemap_url(_origin(url)), policy)
        )
    if sitemap_mode != "only":
        candidates.extend(await _fetch_seed_page_links(url, policy))
    return candidates


async def _discover_via_crawl(
    options: MapOptions, policy: EgressPolicy, policy_filter: filters.FilterPolicy
) -> list[str]:
    """Bounded breadth-first crawl from the seed URL over guarded HTTP -- the
    driver-free substitute for Firecrawl's search-engine (`fireEngineMap`) and
    index sources, which this platform has no equivalent of. Follows only
    in-scope links (per `policy_filter`), never a rendered browser, capped by
    `max_discovery_depth`, `max_crawl_pages`, and bounded concurrency so it
    can't fan out unboundedly. Returns every normalized in-scope link seen."""

    seed_normalized = dedup.normalize_url(
        options.url,
        ignore_query_parameters=options.ignore_query_parameters,
        deduplicate_similar_urls=True,
    )
    if seed_normalized is None:
        return []

    semaphore = asyncio.Semaphore(_CRAWL_FETCH_CONCURRENCY)

    async def _fetch(page_url: str) -> str | None:
        async with semaphore:
            return await _fetch_page_html(page_url, policy)

    discovered: list[str] = []
    discovered_seen: set[str] = set()
    visited: set[str] = {seed_normalized}
    frontier: list[str] = [options.url]
    pages_fetched = 0

    for _ in range(options.max_discovery_depth + 1):
        if not frontier or pages_fetched >= options.max_crawl_pages:
            break
        batch = frontier[: options.max_crawl_pages - pages_fetched]
        pages_fetched += len(batch)
        htmls = await asyncio.gather(*(_fetch(page_url) for page_url in batch))

        next_frontier: list[str] = []
        for page_url, html in zip(batch, htmls, strict=True):
            if html is None:
                continue
            for raw in link_extractor.extract_links(html, page_url):
                normalized = dedup.normalize_url(
                    raw,
                    ignore_query_parameters=options.ignore_query_parameters,
                    deduplicate_similar_urls=True,
                )
                if normalized is None:
                    continue
                if not filters.evaluate(
                    normalized, seed_url=options.url, policy=policy_filter
                ).allowed:
                    continue
                if normalized not in discovered_seen:
                    discovered_seen.add(normalized)
                    discovered.append(normalized)
                if normalized not in visited:
                    visited.add(normalized)
                    next_frontier.append(normalized)
        frontier = next_frontier

    return discovered


async def discover_for_map(options: MapOptions, policy: EgressPolicy) -> list[MapLink]:
    policy_filter = filters.FilterPolicy(
        include_paths=options.include_paths,
        exclude_paths=options.exclude_paths,
        allow_external_links=options.allow_external_links,
        allow_subdomains=options.include_subdomains,
        allow_backward_crawling=True,  # /v1/map has no such restriction concept
        deny_files=True,
    )

    candidates: list[str] = []
    if options.sitemap != "skip":
        candidates.extend(await _gather_sitemap_urls(options.url, policy))
    if options.sitemap != "only":
        candidates.extend(await _discover_via_crawl(options, policy, policy_filter))

    # Firecrawl's `filterByPath`: mapping `example.com/blog` should return
    # blog URLs, not the whole site. Only meaningful for a non-root seed path,
    # and suppressed when the caller opted into external links.
    seed_path = urlparse(options.url).path or "/"
    apply_path_filter = (
        options.filter_by_path
        and not options.allow_external_links
        and seed_path not in ("", "/")
    )

    seen: set[str] = set()
    out: list[MapLink] = []
    for raw in candidates:
        if len(out) >= options.limit:
            break
        normalized = dedup.normalize_url(
            raw,
            ignore_query_parameters=options.ignore_query_parameters,
            deduplicate_similar_urls=True,
        )
        if normalized is None or normalized in seen:
            continue
        if not filters.evaluate(normalized, seed_url=options.url, policy=policy_filter).allowed:
            continue
        if apply_path_filter and not (urlparse(normalized).path or "/").startswith(seed_path):
            continue
        seen.add(normalized)
        out.append(MapLink(url=normalized))

    # Rank within the already-capped set (matches Firecrawl applying its
    # `min(MAX_MAP_LIMIT, limit)` cutoff *before* cosine similarity).
    if options.search:
        out = rank.cosine_rank(out, options.search)
    return out


@dataclass
class CrawlSeedResult:
    urls: list[str]
    """Always includes the normalized seed URL itself (index 0) -- an
    explicit crawl request for a URL is honored regardless of what
    robots.txt/path filters would otherwise say about it; those filters
    govern which *additionally discovered* links get pulled in, not the
    one URL the caller explicitly asked for."""
    robots_parser: RobotFileParser | None
    """Handed back so the crawl-worker's frontier expansion (P4) can reuse
    the same fetched robots.txt for later-discovered URLs instead of
    re-fetching it per task."""


async def discover_for_crawl(options: CrawlOptions, policy: EgressPolicy) -> CrawlSeedResult:
    robots_parser = (
        None if options.ignore_robots_txt else await robots.fetch(_origin(options.url), policy)
    )

    seen: set[str] = set()
    out: list[str] = []
    seed_normalized = dedup.normalize_url(
        options.url,
        ignore_query_parameters=options.ignore_query_parameters,
        deduplicate_similar_urls=options.deduplicate_similar_urls,
    )
    if seed_normalized is not None:
        seen.add(seed_normalized)
        out.append(seed_normalized)

    discovered = await _discover_candidates(options.url, options.sitemap, policy)
    policy_filter = filters.FilterPolicy(
        include_paths=options.include_paths,
        exclude_paths=options.exclude_paths,
        allow_external_links=options.allow_external_links,
        allow_subdomains=options.allow_subdomains,
        allow_backward_crawling=options.allow_backward_crawling,
        deny_files=True,
    )

    for raw in discovered:
        if len(out) >= options.limit:
            break
        normalized = dedup.normalize_url(
            raw,
            ignore_query_parameters=options.ignore_query_parameters,
            deduplicate_similar_urls=options.deduplicate_similar_urls,
        )
        if normalized is None or normalized in seen:
            continue
        decision = filters.evaluate(
            normalized, seed_url=options.url, policy=policy_filter, robots_parser=robots_parser
        )
        if not decision.allowed:
            continue
        seen.add(normalized)
        out.append(normalized)

    return CrawlSeedResult(urls=out, robots_parser=robots_parser)
