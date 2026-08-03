"""`agentpilot.crawl.robots` -- fetched via the egress-guarded client, never
`RobotFileParser.read()`'s own raw fetch."""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from agentpilot.crawl import robots
from agentpilot.spi.egress import EgressPolicy

POLICY = EgressPolicy()


async def test_fetch_parses_disallow_rules(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /private\n", content_type="text/plain"
    )
    parser = await robots.fetch(httpserver.url_for("/").rstrip("/"), POLICY)
    assert parser is not None
    assert robots.is_allowed(parser, httpserver.url_for("/private/x")) is False
    assert robots.is_allowed(parser, httpserver.url_for("/public")) is True


async def test_fetch_returns_none_on_404(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data("not found", status=404)
    parser = await robots.fetch(httpserver.url_for("/").rstrip("/"), POLICY)
    assert parser is None


def test_is_allowed_with_no_parser_means_unrestricted() -> None:
    assert robots.is_allowed(None, "https://example.com/anything") is True


async def test_crawl_delay_is_surfaced(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nCrawl-delay: 2\n", content_type="text/plain"
    )
    parser = await robots.fetch(httpserver.url_for("/").rstrip("/"), POLICY)
    assert robots.crawl_delay(parser) == 2.0


def test_crawl_delay_with_no_parser_is_none() -> None:
    assert robots.crawl_delay(None) is None
