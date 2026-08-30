"""Sitemap fetch + parse -- `<urlset>` and recursive `<sitemapindex>`,
`.xml.gz` decompression, via the egress-guarded client. Structural port of
Firecrawl's `getLinksFromSitemap` orchestration (`WebScraper/sitemap.ts`)
onto stdlib `xml.etree.ElementTree` + `gzip` instead of Firecrawl's Node XML
parser -- same shape, no external dependency.
"""

from __future__ import annotations

import gzip
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

from agentpilot.egress.httpx_guard import guarded_get
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import EgressBlocked

# Safety cap on the number of sitemap documents followed for one discovery
# call, mirroring Firecrawl's own bound against a malicious/misconfigured
# sitemapindex that recursively references itself or fans out unboundedly.
_SITEMAP_LIMIT = 500


def default_sitemap_url(origin: str) -> str:
    return urljoin(origin, "/sitemap.xml")


def _local_tag(el: ElementTree.Element) -> str:
    # Sitemap XML is namespaced (xmlns="http://www.sitemaps.org/schemas/...")
    # -- ElementTree keeps the namespace as a `{uri}tag` prefix on every
    # `.tag`, so comparisons against a bare "urlset"/"loc" need it stripped.
    return el.tag.rsplit("}", 1)[-1]


def _loc_text(el: ElementTree.Element) -> str | None:
    for child in el:
        if _local_tag(child) == "loc" and child.text:
            return child.text.strip()
    return None


class _SitemapWalker:
    def __init__(self, policy: EgressPolicy, limit: int = _SITEMAP_LIMIT) -> None:
        self._policy = policy
        self._limit = limit
        self._visited: set[str] = set()
        self.urls: list[str] = []

    async def walk(self, sitemap_url: str) -> None:
        if sitemap_url in self._visited or len(self._visited) >= self._limit:
            return
        self._visited.add(sitemap_url)

        try:
            resp = await guarded_get(sitemap_url, self._policy, timeout=15.0, follow_redirects=True)
        except (EgressBlocked, httpx.HTTPError):
            return
        if resp.status_code >= 400:
            return

        body = resp.content
        content_type = resp.headers.get("content-type", "")
        if sitemap_url.endswith(".gz") or "gzip" in content_type:
            try:
                body = gzip.decompress(body)
            except OSError:
                return

        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return

        if _local_tag(root) == "sitemapindex":
            children = [
                loc
                for el in root
                if _local_tag(el) == "sitemap" and (loc := _loc_text(el)) is not None
            ]
            for child_url in children:
                await self.walk(child_url)
        elif _local_tag(root) == "urlset":
            for el in root:
                if _local_tag(el) != "url":
                    continue
                loc = _loc_text(el)
                if loc:
                    self.urls.append(loc)


async def fetch_urls(sitemap_url: str, policy: EgressPolicy) -> list[str]:
    """Empty list on any failure (unreachable, not found, malformed XML) --
    same fail-open contract as `robots.fetch()`: a missing/broken sitemap
    means "nothing discovered this way", not a hard error, since crawl/map
    callers fall back to (or combine with) direct link-following regardless.
    """

    walker = _SitemapWalker(policy)
    await walker.walk(sitemap_url)
    return walker.urls
