"""`agentpilot.crawl.seed.discover_for_crawl`/`discover_for_map` -- the
sitemap + shallow-page-links orchestration, combined with `filters`/`dedup`.
"""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from agentpilot.crawl.dedup import normalize_url
from agentpilot.crawl.seed import discover_for_crawl, discover_for_map
from agentpilot.spi.crawl import CrawlOptions, MapOptions
from agentpilot.spi.egress import EgressPolicy

POLICY = EgressPolicy()

SEED_PAGE_HTML = """<html><body>
<a href="/blog/post-1">Post 1</a>
<a href="/about">About</a>
<a href="https://other.com/x">External</a>
</body></html>"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{seed}post-2</loc></url>
</urlset>"""


def _seed_url(httpserver: HTTPServer) -> str:
    return httpserver.url_for("/blog/")


def _normalized_seed_url(httpserver: HTTPServer) -> str:
    # CrawlOptions.deduplicate_similar_urls defaults True, which strips the
    # trailing slash -- what discover_for_crawl actually enqueues the seed
    # as, distinct from the raw URL a caller passed in.
    normalized = normalize_url(_seed_url(httpserver), deduplicate_similar_urls=True)
    assert normalized is not None
    return normalized


async def test_discover_for_map_combines_sitemap_and_page_links(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/blog/").respond_with_data(
        SEED_PAGE_HTML, content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        SITEMAP_XML.format(seed=httpserver.url_for("/blog/")), content_type="application/xml"
    )

    options = MapOptions(url=_seed_url(httpserver))
    links = await discover_for_map(options, POLICY)

    urls = {link.url for link in links}
    assert httpserver.url_for("/blog/post-1") in urls
    assert httpserver.url_for("/blog/post-2") in urls  # from the sitemap
    # External link is filtered out (allow_external_links=False for map).
    assert not any("other.com" in u for u in urls)


async def test_discover_for_map_sitemap_only_skips_page_links(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        SITEMAP_XML.format(seed=httpserver.url_for("/blog/")), content_type="application/xml"
    )
    options = MapOptions(url=_seed_url(httpserver), sitemap="only")
    links = await discover_for_map(options, POLICY)
    assert {link.url for link in links} == {httpserver.url_for("/blog/post-2")}


async def test_discover_for_map_respects_limit(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/blog/").respond_with_data(
        SEED_PAGE_HTML, content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    options = MapOptions(url=_seed_url(httpserver), limit=1)
    links = await discover_for_map(options, POLICY)
    assert len(links) == 1


async def test_discover_for_crawl_always_includes_the_seed_url(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/blog/").respond_with_data("<html></html>", content_type="text/html")
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    options = CrawlOptions(url=_seed_url(httpserver))
    result = await discover_for_crawl(options, POLICY)
    assert result.urls[0] == _normalized_seed_url(httpserver)


async def test_discover_for_crawl_excludes_backward_links_by_default(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/blog/").respond_with_data(
        SEED_PAGE_HTML, content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    options = CrawlOptions(url=_seed_url(httpserver))  # allow_backward_crawling=False default
    result = await discover_for_crawl(options, POLICY)
    assert httpserver.url_for("/blog/post-1") in result.urls
    assert httpserver.url_for("/about") not in result.urls  # backward: outside /blog/


async def test_discover_for_crawl_respects_robots_disallow(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/blog/").respond_with_data(
        SEED_PAGE_HTML, content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /blog/post-1\n", content_type="text/plain"
    )
    options = CrawlOptions(url=_seed_url(httpserver))
    result = await discover_for_crawl(options, POLICY)
    assert httpserver.url_for("/blog/post-1") not in result.urls
    assert result.robots_parser is not None


async def test_discover_for_crawl_ignore_robots_txt_skips_the_fetch(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/blog/").respond_with_data(
        SEED_PAGE_HTML, content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    options = CrawlOptions(url=_seed_url(httpserver), ignore_robots_txt=True)
    result = await discover_for_crawl(options, POLICY)
    assert result.robots_parser is None
    assert httpserver.url_for("/blog/post-1") in result.urls
    # Strict: no request to /robots.txt at all, not just "one that failed
    # open" -- ignore_robots_txt=True must skip the fetch entirely.
    assert not any(req.path == "/robots.txt" for req, _ in httpserver.log)


async def test_discover_for_crawl_respects_limit_including_the_seed(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/blog/").respond_with_data(
        SEED_PAGE_HTML, content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    options = CrawlOptions(url=_seed_url(httpserver), limit=1)
    result = await discover_for_crawl(options, POLICY)
    assert result.urls == [_normalized_seed_url(httpserver)]
