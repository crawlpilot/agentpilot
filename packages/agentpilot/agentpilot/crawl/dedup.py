"""URL normalization for crawl/map dedup. The exact-match half of dedup
needs no code here at all: `crawl_tasks`'s `UNIQUE (job_id, url)` constraint
(`0002_create_jobs.py`) gives exactly-once enqueue via `INSERT ... ON
CONFLICT DO NOTHING` for whatever literal string ends up in the `url`
column -- simpler than Firecrawl's own Redis-`SADD`-based approach. This
module's job is producing *that* canonical string, so two URLs that should
be treated as the same page collapse onto one literal before they ever
reach the constraint.

The URL fragment (`#...`) is always dropped -- it never changes what a
server returns, so keeping it around would only ever produce spurious
"different" URLs, independent of either dedup flag.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(
    url: str, *, ignore_query_parameters: bool = False, deduplicate_similar_urls: bool = False
) -> str | None:
    """`None` if `url` isn't a usable absolute http(s) URL -- callers treat
    that as "drop this candidate", not a hard error, same fail-open
    contract `robots.fetch()`/`sitemap.fetch_urls()` use for fetch failures.
    """

    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if port == _DEFAULT_PORTS.get(scheme):
            netloc = host

    path = parsed.path or "/"
    query = "" if ignore_query_parameters else parsed.query

    if deduplicate_similar_urls:
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        if query:
            # Order-insensitive: ?a=1&b=2 and ?b=2&a=1 are the same page.
            query = urlencode(sorted(parse_qsl(query, keep_blank_values=True)))

    return urlunparse((scheme, netloc, path, "", query, ""))
