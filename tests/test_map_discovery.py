"""`agentpilot.crawl.seed.discover_for_map` + `agentpilot.crawl.rank` -- the
richer discovery ported from Firecrawl: robots.txt-declared sitemaps, the
bounded recursive-crawl fallback, `filter_by_path`, and cosine `search`
ranking. Driver-free, exercised over `pytest_httpserver` like the existing
sitemap tests."""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from agentpilot.crawl import rank, seed
from agentpilot.spi.crawl import MapLink, MapOptions
from agentpilot.spi.egress import EgressPolicy

POLICY = EgressPolicy()


def _urlset(*locs: str) -> str:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


# --- cosine ranking (rank.py) ---


def test_cosine_rank_orders_by_url_relevance() -> None:
    links = [
        MapLink("https://x.test/about"),
        MapLink("https://x.test/team"),
        MapLink("https://x.test/pricing"),
    ]
    ranked = rank.cosine_rank(links, "pricing")
    assert ranked[0].url == "https://x.test/pricing"


def test_cosine_rank_all_punctuation_query_leaves_order_unchanged() -> None:
    links = [MapLink("https://x.test/a"), MapLink("https://x.test/b")]
    assert rank.cosine_rank(links, "!!!") == links


# --- robots.txt-declared sitemaps ---


async def test_discovers_a_sitemap_declared_only_in_robots_txt(httpserver: HTTPServer) -> None:
    declared = httpserver.url_for("/custom-sitemap.xml")
    httpserver.expect_request("/robots.txt").respond_with_data(
        f"User-agent: *\nSitemap: {declared}\n", content_type="text/plain"
    )
    httpserver.expect_request("/custom-sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/from-robots")), content_type="application/xml"
    )
    # The default /sitemap.xml is absent -- the URL is reachable *only* via the
    # robots.txt Sitemap: line, which the prior port ignored.
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    options = MapOptions(url=httpserver.url_for("/"), sitemap="only")
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/from-robots") in urls


# --- bounded recursive-crawl fallback ---


async def test_recursive_crawl_respects_max_discovery_depth(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        f'<a href="{httpserver.url_for("/a")}">a</a>', content_type="text/html"
    )
    httpserver.expect_request("/a").respond_with_data(
        f'<a href="{httpserver.url_for("/b")}">b</a>', content_type="text/html"
    )
    httpserver.expect_request("/b").respond_with_data(
        f'<a href="{httpserver.url_for("/c")}">c</a>', content_type="text/html"
    )
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    options = MapOptions(url=httpserver.url_for("/"), sitemap="skip", max_discovery_depth=1)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/a") in urls  # depth 0's links
    assert httpserver.url_for("/b") in urls  # depth 1's links
    assert httpserver.url_for("/c") not in urls  # depth 2 -- beyond the bound


async def test_recursive_crawl_stops_at_max_crawl_pages(httpserver: HTTPServer) -> None:
    # Seed links to 5 pages; with the page cap at 1 only the seed itself is
    # fetched, so its 5 links are the only ones discovered (none of them get
    # fetched to expand further).
    seed_links = "".join(
        f'<a href="{httpserver.url_for(f"/p{i}")}">p{i}</a>' for i in range(5)
    )
    httpserver.expect_request("/").respond_with_data(seed_links, content_type="text/html")
    for i in range(5):
        httpserver.expect_request(f"/p{i}").respond_with_data(
            f'<a href="{httpserver.url_for(f"/deep{i}")}">deep</a>', content_type="text/html"
        )
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)

    options = MapOptions(
        url=httpserver.url_for("/"), sitemap="skip", max_discovery_depth=5
    )
    options.max_crawl_pages = 1
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/p0") in urls
    assert all(httpserver.url_for(f"/deep{i}") not in urls for i in range(5))


# --- filter_by_path ---


async def test_filter_by_path_restricts_results_to_the_seed_path(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/docs/intro"), httpserver.url_for("/blog/post")),
        content_type="application/xml",
    )
    options = MapOptions(url=httpserver.url_for("/docs"), sitemap="only", filter_by_path=True)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/docs/intro") in urls
    assert httpserver.url_for("/blog/post") not in urls


async def test_filter_by_path_disabled_keeps_off_path_links(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data("nope", status=404)
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        _urlset(httpserver.url_for("/docs/intro"), httpserver.url_for("/blog/post")),
        content_type="application/xml",
    )
    options = MapOptions(url=httpserver.url_for("/docs"), sitemap="only", filter_by_path=False)
    urls = [link.url for link in await seed.discover_for_map(options, POLICY)]
    assert httpserver.url_for("/blog/post") in urls
