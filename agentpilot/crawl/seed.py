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

from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from agentpilot.crawl import dedup, filters, link_extractor, robots, sitemap
from agentpilot.egress.httpx_guard import guarded_get
from agentpilot.spi.crawl import CrawlOptions, MapLink, MapOptions
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import EgressBlocked


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _fetch_seed_page_links(url: str, policy: EgressPolicy) -> list[str]:
    try:
        resp = await guarded_get(url, policy, timeout=15.0, follow_redirects=True)
    except (EgressBlocked, httpx.HTTPError):
        return []
    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
        return []
    return link_extractor.extract_links(resp.text, url)


async def _discover_candidates(url: str, sitemap_mode: str, policy: EgressPolicy) -> list[str]:
    candidates: list[str] = []
    if sitemap_mode != "skip":
        candidates.extend(
            await sitemap.fetch_urls(sitemap.default_sitemap_url(_origin(url)), policy)
        )
    if sitemap_mode != "only":
        candidates.extend(await _fetch_seed_page_links(url, policy))
    return candidates


async def discover_for_map(options: MapOptions, policy: EgressPolicy) -> list[MapLink]:
    candidates = await _discover_candidates(options.url, options.sitemap, policy)
    policy_filter = filters.FilterPolicy(
        include_paths=options.include_paths,
        exclude_paths=options.exclude_paths,
        allow_external_links=False,
        allow_subdomains=options.include_subdomains,
        allow_backward_crawling=True,  # /v1/map has no such restriction concept
        deny_files=True,
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
        seen.add(normalized)
        out.append(MapLink(url=normalized))
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
