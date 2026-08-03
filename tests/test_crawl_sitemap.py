"""`agentpilot.crawl.sitemap.fetch_urls` -- `<urlset>`, recursive
`<sitemapindex>`, and `.xml.gz` decompression, all via the egress-guarded
client."""

from __future__ import annotations

import gzip

from pytest_httpserver import HTTPServer

from agentpilot.crawl import sitemap
from agentpilot.spi.egress import EgressPolicy

POLICY = EgressPolicy()

URLSET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/a</loc></url>
<url><loc>https://example.com/b</loc></url>
</urlset>"""


async def test_parses_a_plain_urlset(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        URLSET_XML, content_type="application/xml"
    )
    urls = await sitemap.fetch_urls(httpserver.url_for("/sitemap.xml"), POLICY)
    assert urls == ["https://example.com/a", "https://example.com/b"]


async def test_recurses_into_a_sitemapindex(httpserver: HTTPServer) -> None:
    index_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{httpserver.url_for("/child.xml")}</loc></sitemap>
</sitemapindex>"""
    httpserver.expect_request("/index.xml").respond_with_data(
        index_xml, content_type="application/xml"
    )
    httpserver.expect_request("/child.xml").respond_with_data(
        URLSET_XML, content_type="application/xml"
    )
    urls = await sitemap.fetch_urls(httpserver.url_for("/index.xml"), POLICY)
    assert urls == ["https://example.com/a", "https://example.com/b"]


async def test_decompresses_gzip_by_extension(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml.gz").respond_with_data(
        gzip.compress(URLSET_XML.encode()), content_type="application/gzip"
    )
    urls = await sitemap.fetch_urls(httpserver.url_for("/sitemap.xml.gz"), POLICY)
    assert urls == ["https://example.com/a", "https://example.com/b"]


async def test_malformed_xml_returns_empty_list(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml").respond_with_data(
        "<not><valid", content_type="application/xml"
    )
    urls = await sitemap.fetch_urls(httpserver.url_for("/sitemap.xml"), POLICY)
    assert urls == []


async def test_404_returns_empty_list(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/sitemap.xml").respond_with_data("nope", status=404)
    urls = await sitemap.fetch_urls(httpserver.url_for("/sitemap.xml"), POLICY)
    assert urls == []


async def test_a_sitemap_referencing_itself_does_not_loop_forever(
    httpserver: HTTPServer,
) -> None:
    self_index_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{httpserver.url_for("/self.xml")}</loc></sitemap>
</sitemapindex>"""
    httpserver.expect_request("/self.xml").respond_with_data(
        self_index_xml, content_type="application/xml"
    )
    urls = await sitemap.fetch_urls(httpserver.url_for("/self.xml"), POLICY)
    assert urls == []


def test_default_sitemap_url_is_at_the_origin_root() -> None:
    assert sitemap.default_sitemap_url("https://example.com") == "https://example.com/sitemap.xml"
