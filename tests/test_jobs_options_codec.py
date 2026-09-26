"""`agentpilot.jobs.options_codec` -- round-trips through the same JSON
serialization Postgres's `jsonb` column applies (`json.loads(json.dumps(...))`
), since a real `Jsonb`-wrapped value goes through exactly that on its way
back out of the database."""

from __future__ import annotations

import json

from agentpilot.crawl.types import BatchScrapeOptions, CrawlOptions
from agentpilot.jobs.options_codec import (
    dump_batch_scrape_options,
    dump_crawl_options,
    dump_scrape_options,
    load_batch_scrape_options,
    load_crawl_options,
    load_scrape_options,
)
from crawlpilot.spi.scrape import ScrapeOptions


def _roundtrip(data: dict) -> dict:
    return json.loads(json.dumps(data))


def test_scrape_options_roundtrip_defaults() -> None:
    original = ScrapeOptions()
    loaded = load_scrape_options(_roundtrip(dump_scrape_options(original)))
    assert loaded.formats == original.formats
    assert loaded.only_main_content == original.only_main_content
    assert loaded.timeout_ms == original.timeout_ms


def test_scrape_options_roundtrip_carries_the_content_filter_options() -> None:
    """`relevance_query`/`citations` must survive the JSONB round trip, or a
    crawl would silently scrape every page with filtering off -- the worker
    reconstructs its options from this column, nowhere else."""

    original = ScrapeOptions(
        formats=("markdown", "fit_markdown", "entities"),
        relevance_query="warranty claim receipt",
        citations=True,
    )
    loaded = load_scrape_options(_roundtrip(dump_scrape_options(original)))
    assert loaded.formats == ("markdown", "fit_markdown", "entities")
    assert loaded.relevance_query == "warranty claim receipt"
    assert loaded.citations is True


def test_scrape_options_load_defaults_the_content_filter_options_off() -> None:
    """Rows written before the two fields existed must load as "filtering
    off", not raise -- there are already `jobs.options` rows in flight."""

    loaded = load_scrape_options({"formats": ["markdown"]})
    assert loaded.relevance_query is None
    assert loaded.citations is False


def test_crawl_options_roundtrip_carries_the_cache_policy() -> None:
    """The worker reconstructs its cache policy from `jobs.options` and nowhere
    else, so a field that does not survive this means a crawl silently runs with
    the deployment default instead of what the caller asked for."""

    original = CrawlOptions(
        url="https://example.com", cache_mode="bypass", max_age_ms=60_000, delay_ms=2_000
    )
    loaded = load_crawl_options(_roundtrip(dump_crawl_options(original)))
    assert loaded.cache_mode == "bypass"
    assert loaded.max_age_ms == 60_000
    assert loaded.delay_ms == 2_000


def test_crawl_options_load_defaults_the_cache_policy() -> None:
    """Rows written before these fields existed must load as "cache on, default
    freshness" rather than raising -- there are `jobs.options` rows in flight."""

    loaded = load_crawl_options({"url": "https://example.com"})
    assert loaded.cache_mode == "enabled"
    assert loaded.max_age_ms is None


def test_batch_scrape_options_roundtrip_carries_the_cache_policy() -> None:
    original = BatchScrapeOptions(
        urls=("https://example.com/a",), cache_mode="read_only", max_age_ms=120_000
    )
    loaded = load_batch_scrape_options(_roundtrip(dump_batch_scrape_options(original)))
    assert loaded.cache_mode == "read_only"
    assert loaded.max_age_ms == 120_000


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
