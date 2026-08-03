"""dict <-> `spi.crawl`/`spi.scrape` option-type conversion for the
`jobs.options` JSONB column. Plain, hand-written (no Pydantic):
`agentpilot.jobs` sits below `agentpilot.gateway` in the layering and must not
import Pydantic request models from it (`gateway.schemas.CrawlRequest` etc.)
-- this codec is the shared ground both `routes/crawl.py` (dump, at job
creation) and `worker_loop.py` (load, per claimed task) can reach without
either depending on the other's layer.

**Known simplification**: `ScrapeOptions.actions` (pre-extract interaction
steps) is not round-tripped -- always empty on load. Pre-extract actions are
much more a single-page `/v1/scrape` primitive than a bulk-crawl one; wiring
the full `Action` union (17 dataclass variants) through this codec is
deferred as a named follow-up, not silently unsupported -- a caller's
`scrape_options.actions` is still stored faithfully in `jobs.options`
(nothing is dropped from the persisted row), only `load_scrape_options`
here doesn't reconstruct them yet.
"""

from __future__ import annotations

from typing import Any

from agentpilot.spi.crawl import BatchScrapeOptions, CrawlOptions
from agentpilot.spi.scrape import ScrapeOptions


def dump_scrape_options(options: ScrapeOptions) -> dict[str, Any]:
    return {
        "formats": list(options.formats),
        "only_main_content": options.only_main_content,
        "include_tags": list(options.include_tags) if options.include_tags else None,
        "exclude_tags": list(options.exclude_tags) if options.exclude_tags else None,
        "timeout_ms": options.timeout_ms,
        "wait_for_ms": options.wait_for_ms,
        "screenshot": options.screenshot,
        "full_page_screenshot": options.full_page_screenshot,
    }


def load_scrape_options(data: dict[str, Any] | None) -> ScrapeOptions:
    data = data or {}
    include_tags = data.get("include_tags")
    exclude_tags = data.get("exclude_tags")
    return ScrapeOptions(
        formats=tuple(data.get("formats") or ("markdown",)),
        only_main_content=data.get("only_main_content", True),
        include_tags=tuple(include_tags) if include_tags else None,
        exclude_tags=tuple(exclude_tags) if exclude_tags else None,
        timeout_ms=data.get("timeout_ms", 30_000),
        wait_for_ms=data.get("wait_for_ms"),
        actions=(),  # see module docstring
        screenshot=data.get("screenshot", False),
        full_page_screenshot=data.get("full_page_screenshot", False),
    )


def dump_crawl_options(options: CrawlOptions) -> dict[str, Any]:
    return {
        "url": options.url,
        "include_paths": list(options.include_paths),
        "exclude_paths": list(options.exclude_paths),
        "max_discovery_depth": options.max_discovery_depth,
        "limit": options.limit,
        "allow_external_links": options.allow_external_links,
        "allow_subdomains": options.allow_subdomains,
        "allow_backward_crawling": options.allow_backward_crawling,
        "ignore_robots_txt": options.ignore_robots_txt,
        "sitemap": options.sitemap,
        "deduplicate_similar_urls": options.deduplicate_similar_urls,
        "ignore_query_parameters": options.ignore_query_parameters,
        "delay_ms": options.delay_ms,
        "max_concurrency": options.max_concurrency,
        "scrape_options": dump_scrape_options(options.scrape_options),
    }


def load_crawl_options(data: dict[str, Any]) -> CrawlOptions:
    return CrawlOptions(
        url=data["url"],
        include_paths=tuple(data.get("include_paths") or ()),
        exclude_paths=tuple(data.get("exclude_paths") or ()),
        max_discovery_depth=data.get("max_discovery_depth"),
        limit=data.get("limit", 10_000),
        allow_external_links=data.get("allow_external_links", False),
        allow_subdomains=data.get("allow_subdomains", False),
        allow_backward_crawling=data.get("allow_backward_crawling", False),
        ignore_robots_txt=data.get("ignore_robots_txt", False),
        sitemap=data.get("sitemap", "include"),
        deduplicate_similar_urls=data.get("deduplicate_similar_urls", True),
        ignore_query_parameters=data.get("ignore_query_parameters", False),
        delay_ms=data.get("delay_ms"),
        max_concurrency=data.get("max_concurrency", 10),
        scrape_options=load_scrape_options(data.get("scrape_options")),
    )


def dump_batch_scrape_options(options: BatchScrapeOptions) -> dict[str, Any]:
    return {
        "urls": list(options.urls),
        "scrape_options": dump_scrape_options(options.scrape_options),
    }


def load_batch_scrape_options(data: dict[str, Any]) -> BatchScrapeOptions:
    return BatchScrapeOptions(
        urls=tuple(data["urls"]),
        scrape_options=load_scrape_options(data.get("scrape_options")),
    )
