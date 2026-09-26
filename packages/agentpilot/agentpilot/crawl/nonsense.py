"""Utility-URL filtering: the things a discovery source returns that nobody
asked to scrape.

The moment discovery stops being "the seed page's links plus its sitemap" and
starts including Common Crawl and Wayback, the result set fills with machinery.
Wayback in particular has archived every `sitemap.xml`, every webpack chunk and
every `favicon.ico` a site ever served, and a caller asking to map
`example.com` does not want four hundred `/_next/static/chunks/*.js` entries in
their answer.

Separate from `filters.py` on purpose. That module answers a *scope* question --
is this URL inside the crawl the caller described -- and its answers depend on
`FilterPolicy`. This one answers a *kind* question with no policy at all: a
`.woff2` is not a page on anybody's site. Keeping them apart means
`allow_external_links=True` widens scope without also admitting every stylesheet
on the internet.

Ported from crawl4ai's `DomainMapper._is_nonsense` and `AsyncUrlSeeder`'s
`filter_nonsense_urls` (Apache-2.0; crawl4ai 0.9.4), with the asset list
extended and the two Wayback-specific checks kept -- those exist because that
source really does return URLs with encoded newlines in them.
"""

from __future__ import annotations

from urllib.parse import urlparse

_ASSET_EXTENSIONS = frozenset(
    {
        # Stylesheets and scripts
        ".css", ".js", ".mjs", ".cjs", ".map",
        # Fonts
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        # Images
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
        ".bmp", ".tiff",
        # Media
        ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".avi", ".mov", ".m4a",
        ".m3u8", ".ts",
        # Archives and binaries
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".rar", ".7z", ".dmg", ".exe",
        ".apk", ".deb", ".rpm", ".jar", ".wasm",
    }
)
"""Deliberately does **not** include `.pdf`, `.doc(x)`, `.xls(x)` or `.csv`.
Those are documents a caller may legitimately want -- once PDF extraction lands
they are pages in every sense that matters -- whereas nothing downstream can do
anything useful with a `.woff2`.

Note this is currently forward-looking rather than effective: `filters
._DENIED_EXTENSIONS` still drops those same extensions on both the map and crawl
paths, with its own "no pdf/document engine yet" note. So a PDF survives *this*
filter and is dropped by that one. Both lists have to change for documents to
come back, and this module is not the place that decides it."""

_NONSENSE_FILENAMES = frozenset(
    {
        "/robots.txt", "/ads.txt", "/app-ads.txt", "/security.txt",
        "/humans.txt", "/favicon.ico", "/manifest.json", "/browserconfig.xml",
        "/crossdomain.xml", "/service-worker.js", "/sw.js", "/rss.xml",
        "/atom.xml", "/feed.xml", "/opensearch.xml",
    }
)

_NONSENSE_PATH_FRAGMENTS = (
    "/_next/",
    "/_nuxt/",
    "/webpack",
    "/.well-known/",
    "/wp-json/",
    "/cdn-cgi/",
    "/__webpack",
    "/static/chunks/",
)

_WAYBACK_GARBAGE = ("%5C", "%0A", "%0D", "\\n", "\\r", "%00")
"""Encoded backslashes, newlines and nulls. Not hypothetical -- the CDX API
returns URLs containing these, and they are not fetchable."""


def is_nonsense(url: str) -> bool:
    """True for a URL that is site machinery rather than a page.

    Errs toward keeping things. A false positive here silently removes a page
    the caller wanted and they have no way to see why; a false negative costs
    them one junk row they can ignore.
    """

    try:
        parsed = urlparse(url)
    except ValueError:
        return True

    if any(marker in url for marker in _WAYBACK_GARBAGE):
        return True

    path = (parsed.path or "/").lower()

    if path in _NONSENSE_FILENAMES:
        return True
    if any(fragment in path for fragment in _NONSENSE_PATH_FRAGMENTS):
        return True
    if any(path.endswith(ext) for ext in _ASSET_EXTENSIONS):
        return True
    # Sitemaps in any of their spellings -- `/sitemap.xml`, `/sitemap_index.xml`,
    # `/sitemaps/products-1.xml.gz`. They are how discovery *works*; they are not
    # results of it.
    if "sitemap" in path and path.endswith((".xml", ".xml.gz", ".txt", ".gz")):
        return True
    # Dotfiles and dot-directories at any depth (`/.git/config`, `/.env`).
    if any(part.startswith(".") for part in path.split("/") if part not in ("", ".", "..")):
        return True

    return False
