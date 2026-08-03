"""Given a just-scraped page's HTML, produces the next batch of candidate
URLs for the crawl-worker's frontier expansion (`agentpilot.jobs.worker_loop
.CrawlWorkerLoop`) -- link-extract, normalize, and filter, reusing
`link_extractor`/`dedup`/`filters` directly rather than going through
`seed.py` again (`seed.py`'s job is the one-time *initial* batch; this is
the ongoing, depth-tracked expansion as each queued page actually gets
scraped). Not a Firecrawl file 1:1 -- glue specific to this platform's
queue-driven crawl loop, kept here (not inline in `worker_loop.py`) so it
stays a pure, zero-browser-testable function.
"""

from __future__ import annotations

from urllib.robotparser import RobotFileParser

from agentpilot.crawl import dedup, filters, link_extractor
from agentpilot.spi.crawl import CrawlOptions


def expand_frontier(
    *,
    html: str,
    page_url: str,
    depth: int,
    seed_url: str,
    options: CrawlOptions,
    robots_parser: RobotFileParser | None,
) -> list[str]:
    """`depth` is the depth of `page_url` itself (the page just scraped);
    returned URLs are implicitly one level deeper. Callers enforce
    `max_discovery_depth` by simply not calling this at all once a task's
    own depth already reached the limit -- this function has no opinion on
    depth beyond using `page_url`'s to resolve relative links."""

    if options.max_discovery_depth is not None and depth >= options.max_discovery_depth:
        return []

    policy = filters.FilterPolicy(
        include_paths=options.include_paths,
        exclude_paths=options.exclude_paths,
        allow_external_links=options.allow_external_links,
        allow_subdomains=options.allow_subdomains,
        allow_backward_crawling=options.allow_backward_crawling,
        deny_files=True,
    )

    seen: set[str] = set()
    out: list[str] = []
    for raw in link_extractor.extract_links(html, page_url):
        normalized = dedup.normalize_url(
            raw,
            ignore_query_parameters=options.ignore_query_parameters,
            deduplicate_similar_urls=options.deduplicate_similar_urls,
        )
        if normalized is None or normalized in seen:
            continue
        decision = filters.evaluate(
            normalized, seed_url=seed_url, policy=policy, robots_parser=robots_parser
        )
        if not decision.allowed:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
