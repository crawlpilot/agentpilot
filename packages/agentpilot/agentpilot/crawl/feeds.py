"""RSS and Atom feeds as a discovery source.

A feed is a site telling you, in a machine-readable form it maintains itself,
which of its pages are the content ones and when each last changed. For any
publication, blog, docs site or changelog, that is better information than a
sitemap: sitemaps list everything including the login page, and feeds list what
the site considers worth reading.

The `<updated>`/`<pubDate>` timestamps also feed `scorers.freshness`, which is the
one signal that lets a bounded crawl spend its budget on this month's pages
rather than 2019's.

Ported from crawl4ai's `DomainMapper._discover_feeds` / `_parse_feed_xml`
(Apache-2.0; crawl4ai 0.9.4). Two differences: feed URLs declared in the
homepage's `<link rel="alternate">` are honoured (the conventional paths below
miss every site that puts its feed anywhere else), and entry dates are returned
rather than discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx
import structlog

from crawlpilot.egress.httpx_guard import guarded_get
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

log = structlog.get_logger(__name__)

CONVENTIONAL_PATHS = (
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/blog/feed",
    "/blog/rss.xml",
    "/news/feed",
    "/.rss",
)

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class FeedEntry:
    url: str
    title: str | None
    published: datetime | None
    """Parsed from `<updated>`, `<published>` or `<pubDate>` depending on the
    feed dialect. Feeds this platform has no other way of dating -- which is
    every page discovered by any other source -- score neutrally in
    `scorers.freshness`; these score on their actual age."""


async def discover(
    origin: str,
    policy: EgressPolicy,
    *,
    declared_urls: tuple[str, ...] = (),
    timeout_seconds: float = 10.0,
    max_feeds: int = 4,
) -> list[FeedEntry]:
    """Entries from this origin's feeds.

    `declared_urls` are feed URLs found in a page's `<link rel="alternate">`, and
    are tried first: a site that declares its feed is telling you where it is,
    and guessing paths at a site that already answered the question is wasted
    requests.
    """

    candidates: list[str] = []
    for url in declared_urls:
        if url not in candidates:
            candidates.append(url)
    for path in CONVENTIONAL_PATHS:
        url = f"{origin.rstrip('/')}{path}"
        if url not in candidates:
            candidates.append(url)

    entries: list[FeedEntry] = []
    seen_urls: set[str] = set()
    feeds_found = 0

    for candidate in candidates:
        if feeds_found >= max_feeds:
            break
        parsed = await _fetch_feed(candidate, policy, timeout_seconds=timeout_seconds)
        if not parsed:
            continue
        feeds_found += 1
        for entry in parsed:
            if entry.url not in seen_urls:
                seen_urls.add(entry.url)
                entries.append(entry)

    return entries


async def _fetch_feed(
    url: str, policy: EgressPolicy, *, timeout_seconds: float
) -> list[FeedEntry]:
    try:
        response = await guarded_get(
            url, policy, timeout=timeout_seconds, follow_redirects=True
        )
    except (EgressBlocked, httpx.HTTPError):
        return []
    if response.status_code != 200:
        return []
    content_type = response.headers.get("content-type", "").lower()
    # Many sites serve a feed as `text/html` (misconfigured) and many serve their
    # 404 page as `application/xml`, so the content type is a weak hint at best --
    # the parse below is the real test. This only skips the obviously-wrong.
    if "html" in content_type and "xml" not in content_type:
        return []
    return parse(response.text, base_url=url)


def parse(xml_text: str, *, base_url: str) -> list[FeedEntry]:
    """Entries from an RSS 2.0 or Atom document. `[]` for anything unparseable --
    which includes every HTML 404 page served at a guessed feed path, and is
    exactly how those are rejected."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    entries: list[FeedEntry] = []
    # Atom
    for item in root.findall(f".//{_ATOM_NS}entry"):
        url = _atom_link(item, base_url)
        if url is None:
            continue
        entries.append(
            FeedEntry(
                url=url,
                title=_text(item, f"{_ATOM_NS}title"),
                published=_first_date(
                    item, (f"{_ATOM_NS}updated", f"{_ATOM_NS}published")
                ),
            )
        )
    # RSS 2.0
    for item in root.findall(".//item"):
        link = _text(item, "link")
        if not link:
            continue
        entries.append(
            FeedEntry(
                url=urljoin(base_url, link),
                title=_text(item, "title"),
                published=_first_date(item, ("pubDate", "date")),
            )
        )
    return entries


def _atom_link(item: ElementTree.Element, base_url: str) -> str | None:
    """Atom puts the URL in `<link href>`, and a well-formed entry may carry
    several -- `alternate` is the page, `enclosure` is a media file, `replies` is a
    comment feed. Taking the first one indiscriminately returns podcast MP3s."""

    fallback: str | None = None
    for link in item.findall(f"{_ATOM_NS}link"):
        href = link.get("href")
        if not href:
            continue
        rel = (link.get("rel") or "alternate").lower()
        if rel == "alternate":
            return urljoin(base_url, href)
        if fallback is None and rel not in ("enclosure", "replies", "edit", "self"):
            fallback = urljoin(base_url, href)
    if fallback is not None:
        return fallback
    identifier = _text(item, f"{_ATOM_NS}id")
    # An Atom `<id>` is required to be a URI but is not required to be a URL; only
    # usable when it happens to be one.
    if identifier and identifier.startswith(("http://", "https://")):
        return identifier
    return None


def _text(item: ElementTree.Element, tag: str) -> str | None:
    element = item.find(tag)
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _first_date(item: ElementTree.Element, tags: tuple[str, ...]) -> datetime | None:
    for tag in tags:
        raw = _text(item, tag)
        if raw:
            parsed = _parse_date(raw)
            if parsed is not None:
                return parsed
    return None


def _parse_date(raw: str) -> datetime | None:
    """RSS uses RFC 2822 (`Tue, 01 Sep 2026 10:00:00 GMT`), Atom uses RFC 3339
    (`2026-09-01T10:00:00Z`). Both appear in the wild in each other's feeds, so
    both are tried."""

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        # A naive timestamp compared against an aware `now()` raises. Feeds
        # omitting an offset are common enough that assuming UTC beats dropping
        # the date.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
