"""URL-candidate filtering: file-extension denylist, domain policy
(external links / subdomains / backward crawling), include/exclude path
regex, and robots.txt delegation -- structural port of Firecrawl's
`WebCrawler.filterLinks` (`crawler.ts`), the most directly reusable piece of
its crawl logic. Option-agnostic (takes a `FilterPolicy`, not `CrawlOptions`
directly) so both crawl and map seeding (`seed.py`) can drive it from
whichever subset of fields their own options type actually has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from agentpilot.crawl import robots as robots_mod

# Extensions AgentPilot's engine has no way to meaningfully render/extract
# today -- no pdf/document engine exists yet (see spi.scrape.Document
# .raw_html's docstring for the same "engine can't do this yet" honesty).
# Revisit once one lands; until then, following these links just produces
# scrape failures downstream.
_DENIED_EXTENSIONS = frozenset(
    {
        # images
        "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tiff",
        # stylesheets/scripts/fonts
        "css", "js", "mjs", "woff", "woff2", "ttf", "eot", "map",
        # archives/binaries
        "zip", "tar", "gz", "rar", "7z", "exe", "dmg", "apk", "iso",
        # media
        "mp3", "mp4", "avi", "mov", "wav", "webm", "ogg", "flac",
        # documents (no pdf/document engine yet)
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "rtf", "odt",
        # data
        "xml", "json", "csv", "rss", "atom",
    }
)


@dataclass
class FilterPolicy:
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    allow_external_links: bool = False
    allow_subdomains: bool = False
    allow_backward_crawling: bool = True
    """Default `True` (unrestricted) since that's the right default for
    `/v1/map`'s `MapOptions`, which has no such field at all. `seed.py`
    always sets this explicitly from `CrawlOptions.allow_backward_crawling`
    (default `False`) when building a policy for crawl, so crawl's actual
    default stays path-restricted -- this dataclass default is never what
    crawl silently falls back to."""
    deny_files: bool = True


@dataclass
class FilterDecision:
    allowed: bool
    reason: str | None = None


def _registrable_domain(hostname: str) -> str:
    """No public-suffix-list for MVP -- last two labels ("example.com" out
    of "www.example.com"/"blog.example.com") covers the overwhelming
    majority of real sites. Known miss: multi-part TLDs like
    "example.co.uk" collapse to "co.uk", which would treat two unrelated
    *.co.uk sites as "the same domain" for subdomain purposes -- a real
    public-suffix-list is the fix, deliberately deferred as a named
    follow-up rather than adding that dependency now."""

    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def _is_denied_file(path: str) -> bool:
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return False
    return last_segment.rsplit(".", 1)[-1].lower() in _DENIED_EXTENSIONS


def _is_backward(candidate_path: str, seed_path: str) -> bool:
    seed_prefix = seed_path if seed_path.endswith("/") else seed_path.rsplit("/", 1)[0] + "/"
    return not candidate_path.startswith(seed_prefix)


def evaluate(
    candidate_url: str,
    *,
    seed_url: str,
    policy: FilterPolicy,
    robots_parser: RobotFileParser | None = None,
) -> FilterDecision:
    """`robots_parser=None` skips the robots check entirely -- used both for
    `/v1/map` (which has no robots concept at all) and for crawl callers
    that already resolved `ignore_robots_txt=True` and so never fetched a
    parser in the first place; either way, "no parser" and "robots.txt
    doesn't restrict this" collapse to the same behavior."""

    candidate = urlparse(candidate_url)
    seed = urlparse(seed_url)

    if candidate.scheme not in ("http", "https"):
        return FilterDecision(False, "non-http(s) scheme")

    if policy.deny_files and _is_denied_file(candidate.path):
        return FilterDecision(False, "denied file extension")

    candidate_host = candidate.hostname or ""
    seed_host = seed.hostname or ""
    if candidate_host != seed_host:
        if _registrable_domain(candidate_host) == _registrable_domain(seed_host):
            if not policy.allow_subdomains:
                return FilterDecision(False, "different subdomain, allow_subdomains=False")
        elif not policy.allow_external_links:
            return FilterDecision(False, "external domain, allow_external_links=False")

    if not policy.allow_backward_crawling and _is_backward(
        candidate.path or "/", seed.path or "/"
    ):
        return FilterDecision(False, "outside seed path, allow_backward_crawling=False")

    if policy.include_paths and not any(
        re.search(p, candidate.path) for p in policy.include_paths
    ):
        return FilterDecision(False, "does not match include_paths")

    if policy.exclude_paths and any(re.search(p, candidate.path) for p in policy.exclude_paths):
        return FilterDecision(False, "matches exclude_paths")

    if robots_parser is not None and not robots_mod.is_allowed(robots_parser, candidate_url):
        return FilterDecision(False, "disallowed by robots.txt")

    return FilterDecision(True)
