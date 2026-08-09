"""`agentpilot.session.http_fetch.fetch_via_http` -- the basic-tier HTTP
fast-path. Driven by an `httpx.MockTransport`, so no network or real proxy."""

from __future__ import annotations

import httpx
import pytest

from agentpilot.session.http_fetch import fetch_via_http, proxy_url
from agentpilot.spi.errors import ChallengeDetected
from agentpilot.spi.proxy import ProxyEndpoint
from agentpilot.spi.scrape import ScrapeOptions

_OK_HTML = (
    "<html><head><title>Widget</title></head><body>"
    "<h1>Widget</h1><a href='/x'>link</a>" + ("<p>content</p>" * 200) + "</body></html>"
)


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_extracts_requested_formats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=_OK_HTML)

    result = await fetch_via_http(
        url="https://example.com/p",
        formats=("markdown", "html"),
        options=ScrapeOptions(formats=("markdown", "html")),
        headers={"User-Agent": "x"},
        client=_client(handler),
    )
    assert result.status_code == 200
    assert result.page_title == "Widget"
    assert len(result.extracts) == 2
    assert "Widget" in result.extracts[0]  # markdown
    assert result.soft_verdict is None


async def test_fetch_raises_on_hard_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html><body>403 Forbidden</body></html>")

    with pytest.raises(ChallengeDetected) as exc:
        await fetch_via_http(
            url="https://example.com/p",
            formats=("markdown",),
            options=ScrapeOptions(),
            headers={"User-Agent": "x"},
            client=_client(handler),
        )
    assert exc.value.scope == "privacy"


async def test_fetch_marks_soft_verdict_but_returns_content() -> None:
    # A 429 is RATE_LIMITED -> CRAWL scope: not raised, flagged for a soft retry.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, html=_OK_HTML)

    result = await fetch_via_http(
        url="https://example.com/p",
        formats=("markdown",),
        options=ScrapeOptions(),
        headers={"User-Agent": "x"},
        client=_client(handler),
    )
    assert result.soft_verdict == "rate_limited"
    assert result.extracts  # content still returned


async def test_fetch_follows_redirects_and_classifies_final_url() -> None:
    # A landed /blocked URL is a robot check (PRIVACY) even on a 200.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><body>ok</body></html>", request=request)

    with pytest.raises(ChallengeDetected):
        await fetch_via_http(
            url="https://www.walmart.com/blocked?url=abc",
            formats=("markdown",),
            options=ScrapeOptions(),
            headers={"User-Agent": "x"},
            client=_client(handler),
        )


def test_proxy_url_includes_credentials() -> None:
    p = ProxyEndpoint(scheme="http", host="gw", port=8000, username="u", password="pw")
    assert proxy_url(p) == "http://u:pw@gw:8000"
    assert proxy_url(ProxyEndpoint(scheme="http", host="gw", port=8000)) == "http://gw:8000"
