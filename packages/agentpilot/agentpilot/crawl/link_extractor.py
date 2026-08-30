"""`<a href>` extraction from raw HTML -- stdlib `html.parser`, not
`lxml`/Cheerio: this package stays driver-extras-free (see the package
docstring). Structural equivalent of Firecrawl's Cheerio-based link
extraction fallback (`extractLinksFromHTMLCheerio` in `crawler.ts`) -- its
primary path is a compiled Rust crate, not portable here.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = next((v for k, v in attrs if k == "href" and v), None)
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        self.links.append(urljoin(self._base_url, href))


def extract_links(html: str, base_url: str) -> list[str]:
    """Resolves every `href` against `base_url` (so relative links become
    absolute) and drops non-navigable ones (`javascript:`, `mailto:`,
    `tel:`, bare same-page `#fragment` links). Malformed HTML is tolerated
    the way browsers tolerate it -- `HTMLParser` doesn't raise on it, it just
    does its best, which is exactly the behavior wanted here."""

    parser = _LinkParser(base_url)
    parser.feed(html)
    return parser.links
