"""The crawl4ai content-filter port's *wiring*, end to end through the HTTP
layer's pure functions -- no database, no browser.

The filters themselves are tested in `packages/crawlpilot/tests/
test_content_filters.py`. What this file covers is the part that is easy to get
wrong and invisible when it is: a new `ScrapeOptions` field that the request
model accepts but the route never reads, or a `Document` field the engine sets
and `DocumentOut` silently drops. Both would look like "the filter doesn't
work" from outside, with nothing failing anywhere.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentpilot.gateway.routes.crawl import _document_out, _to_crawl_options
from agentpilot.gateway.schemas import CrawlRequest, ScrapeOptionsIn, ScrapeRequest
from crawlpilot.spi.scrape import Document, DocumentMetadata


def test_scrape_request_accepts_the_new_formats_and_options() -> None:
    req = ScrapeRequest(
        tenant="t",
        url="https://example.com",
        formats=["markdown", "fit_markdown", "entities"],
        relevance_query="warranty claim receipt",
        citations=True,
    )
    assert req.formats == ["markdown", "fit_markdown", "entities"]
    assert req.relevance_query == "warranty claim receipt"
    assert req.citations is True


def test_scrape_request_defaults_leave_the_filters_off() -> None:
    req = ScrapeRequest(tenant="t", url="https://example.com")
    assert req.formats == ["markdown"]
    assert req.relevance_query is None
    assert req.citations is False


def test_scrape_request_still_rejects_an_unknown_format() -> None:
    with pytest.raises(ValidationError):
        ScrapeRequest(tenant="t", url="https://example.com", formats=["fit-markdown"])


def test_crawl_request_carries_the_options_down_to_scrape_options() -> None:
    """`ScrapeOptionsIn` is a separate model from `ScrapeRequest`, so a field
    added to one and not the other is a silent gap on the bulk path."""

    req = CrawlRequest(
        tenant="t",
        url="https://example.com",
        scrape_options=ScrapeOptionsIn(
            formats=["fit_markdown"],
            relevance_query="return policy",
            citations=True,
        ),
    )
    options = _to_crawl_options(req).scrape_options
    assert options.formats == ("fit_markdown",)
    assert options.relevance_query == "return policy"
    assert options.citations is True


def test_scrape_request_accepts_the_cache_policy() -> None:
    req = ScrapeRequest(
        tenant="t",
        url="https://example.com",
        cache_mode="bypass",
        max_age_ms=60_000,
    )
    assert req.cache_mode == "bypass"
    assert req.max_age_ms == 60_000


def test_scrape_request_cache_defaults_are_on_with_no_explicit_age() -> None:
    req = ScrapeRequest(tenant="t", url="https://example.com")
    assert req.cache_mode == "enabled"
    assert req.max_age_ms is None


def test_scrape_request_rejects_an_unknown_cache_mode() -> None:
    with pytest.raises(ValidationError):
        ScrapeRequest(tenant="t", url="https://example.com", cache_mode="sometimes")


def test_crawl_request_carries_the_cache_policy_into_crawl_options() -> None:
    req = CrawlRequest(
        tenant="t",
        url="https://example.com",
        cache_mode="write_only",
        max_age_ms=30_000,
        delay_ms=1_500,
    )
    options = _to_crawl_options(req)
    assert options.cache_mode == "write_only"
    assert options.max_age_ms == 30_000
    assert options.delay_ms == 1_500


def _document(**kwargs: object) -> Document:
    return Document(
        document_id="d1",
        url="https://example.com",
        metadata=DocumentMetadata(
            title=None,
            status_code=200,
            tier_used="auto",
            node_id="n1",
            duration_ms=1.0,
            source_url="https://example.com",
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def test_document_out_carries_fit_markdown_and_entities() -> None:
    out = _document_out(
        _document(
            markdown="# full page",
            fit_markdown="# just the good bit",
            entities={"email": ["hi@example.com"]},
        )
    )
    assert out.markdown == "# full page"
    assert out.fit_markdown == "# just the good bit"
    assert out.entities == {"email": ["hi@example.com"]}


def test_document_out_leaves_the_new_fields_none_when_unrequested() -> None:
    out = _document_out(_document(markdown="# full page"))
    assert out.fit_markdown is None
    assert out.entities is None
