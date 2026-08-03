"""`agentpilot.jobs.options_codec` -- round-trips through the same JSON
serialization Postgres's `jsonb` column applies (`json.loads(json.dumps(...))`
), since a real `Jsonb`-wrapped value goes through exactly that on its way
back out of the database."""

from __future__ import annotations

import json

from agentpilot.jobs.options_codec import (
    dump_batch_scrape_options,
    dump_crawl_options,
    dump_scrape_options,
    load_batch_scrape_options,
    load_crawl_options,
    load_scrape_options,
)
from agentpilot.spi.crawl import BatchScrapeOptions, CrawlOptions
from agentpilot.spi.scrape import ScrapeOptions


def _roundtrip(data: dict) -> dict:
    return json.loads(json.dumps(data))


def test_scrape_options_roundtrip_defaults() -> None:
    original = ScrapeOptions()
    loaded = load_scrape_options(_roundtrip(dump_scrape_options(original)))
    assert loaded.formats == original.formats
    assert loaded.only_main_content == original.only_main_content
    assert loaded.timeout_ms == original.timeout_ms


def test_scrape_options_roundtrip_non_defaults() -> None:
    original = ScrapeOptions(
        formats=("markdown", "html"),
        only_main_content=False,
        include_tags=("article",),
        exclude_tags=("nav", "footer"),
        timeout_ms=15_000,
        wait_for_ms=500,
        screenshot=True,
        full_page_screenshot=True,
    )
    loaded = load_scrape_options(_roundtrip(dump_scrape_options(original)))
    assert loaded.formats == original.formats
    assert loaded.include_tags == original.include_tags
    assert loaded.exclude_tags == original.exclude_tags
    assert loaded.wait_for_ms == original.wait_for_ms
    assert loaded.screenshot is True
    assert loaded.full_page_screenshot is True


def test_crawl_options_roundtrip() -> None:
    original = CrawlOptions(
        url="https://example.com/blog/",
        include_paths=(r"^/blog/",),
        exclude_paths=(r"/drafts/",),
        max_discovery_depth=3,
        limit=500,
        allow_external_links=True,
        allow_subdomains=True,
        allow_backward_crawling=True,
        ignore_robots_txt=True,
        sitemap="only",
        deduplicate_similar_urls=False,
        ignore_query_parameters=True,
        delay_ms=250,
        max_concurrency=20,
        scrape_options=ScrapeOptions(formats=("html",)),
    )
    loaded = load_crawl_options(_roundtrip(dump_crawl_options(original)))
    assert loaded.url == original.url
    assert loaded.include_paths == original.include_paths
    assert loaded.exclude_paths == original.exclude_paths
    assert loaded.max_discovery_depth == original.max_discovery_depth
    assert loaded.limit == original.limit
    assert loaded.allow_external_links == original.allow_external_links
    assert loaded.allow_subdomains == original.allow_subdomains
    assert loaded.allow_backward_crawling == original.allow_backward_crawling
    assert loaded.ignore_robots_txt == original.ignore_robots_txt
    assert loaded.sitemap == original.sitemap
    assert loaded.deduplicate_similar_urls == original.deduplicate_similar_urls
    assert loaded.ignore_query_parameters == original.ignore_query_parameters
    assert loaded.delay_ms == original.delay_ms
    assert loaded.max_concurrency == original.max_concurrency
    assert loaded.scrape_options.formats == ("html",)


def test_crawl_options_roundtrip_defaults_when_optional_fields_absent() -> None:
    loaded = load_crawl_options({"url": "https://example.com"})
    assert loaded.url == "https://example.com"
    assert loaded.limit == 10_000
    assert loaded.max_discovery_depth is None
    assert loaded.sitemap == "include"


def test_batch_scrape_options_roundtrip() -> None:
    original = BatchScrapeOptions(
        urls=("https://a.example", "https://b.example"),
        scrape_options=ScrapeOptions(formats=("text",)),
    )
    loaded = load_batch_scrape_options(_roundtrip(dump_batch_scrape_options(original)))
    assert loaded.urls == original.urls
    assert loaded.scrape_options.formats == ("text",)
