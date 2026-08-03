"""`agentpilot.session.ephemeral.run_ephemeral_scrape` -- real Patchright
context, real in-memory `Registry`. `routes/scrape.py`'s own tests
(`test_scrape_route.py`) cover this indirectly through the HTTP layer; this
module tests the shared function directly, since it now has two independent
callers (`routes/scrape.py` and, from this pass on, the crawl-worker loop).
"""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.session.ephemeral import run_ephemeral_scrape
from agentpilot.session.registry import Registry
from agentpilot.spi.scrape import ScrapeOptions

ARTICLE_HTML = """<html><body>
<article>
<h1>Ephemeral Scrape Article</h1>
<p>This is the first paragraph of real body content, long enough and
distinct enough that trafilatura should treat it as the main article rather
than boilerplate noise.</p>
<p>A second paragraph continues with different wording so extraction has
real multi-paragraph content to check against.</p>
</article>
</body></html>"""


async def test_run_ephemeral_scrape_returns_markdown_and_no_screenshot_by_default(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    registry = Registry()

    document, screenshot_bytes = await run_ephemeral_scrape(
        tenant="acme",
        domain="127.0.0.1",
        url=httpserver.url_for("/"),
        options=ScrapeOptions(),
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
    )

    assert document.markdown is not None
    assert "Ephemeral Scrape Article" in document.markdown
    assert document.error is None
    assert screenshot_bytes is None
    assert await registry.snapshot() == []


async def test_run_ephemeral_scrape_captures_a_screenshot_when_requested(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    registry = Registry()

    _document, screenshot_bytes = await run_ephemeral_scrape(
        tenant="acme",
        domain="127.0.0.1",
        url=httpserver.url_for("/"),
        options=ScrapeOptions(screenshot=True),
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
    )

    assert screenshot_bytes is not None
    assert screenshot_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes


async def test_run_ephemeral_scrape_supports_multiple_formats_in_one_call(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    registry = Registry()

    document, _screenshot = await run_ephemeral_scrape(
        tenant="acme",
        domain="127.0.0.1",
        url=httpserver.url_for("/"),
        options=ScrapeOptions(formats=("markdown", "html")),
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
    )

    assert document.markdown is not None
    assert document.html is not None
    assert "<html" in document.html.lower()
