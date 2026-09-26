"""Common Crawl as a discovery source: URLs someone else already crawled.

The index holds a few hundred billion URLs collected by a public non-profit
crawler. Querying it for a domain costs one HTTP request and returns paths a
site has never linked from its homepage and never put in a sitemap -- old
articles, deep product pages, sections reachable only through a search form.
That is the class of URL `/v1/map` currently cannot find at all.

The cost is freshness: an index is a snapshot, typically weeks to months old. A
URL from here may be gone. That is what `soft404` and `head`'s liveness check are
for, and it is why this is one source among several rather than the source.

Ported from crawl4ai's `AsyncUrlSeeder`'s `cc` source (Apache-2.0; crawl4ai
0.9.4). Two differences:

1. **The index id is cached in-process, not on disk.** Upstream writes it to
   `~/.crawl4ai/`, which is right for a CLI on someone's laptop and wrong for a
   container that may be immutable and is certainly not the same container next
   week. A module-level value with a TTL costs one extra request per worker
   lifetime.
2. **Bounded response reading.** A query for a large domain can return tens of
   megabytes of JSONL. `max_urls` stops parsing rather than materializing all of
   it and slicing afterwards.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx
import structlog

from crawlpilot.egress.httpx_guard import guarded_get
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

log = structlog.get_logger(__name__)

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
INDEX_URL_TEMPLATE = "https://index.commoncrawl.org/{index_id}-index"

_INDEX_TTL_SECONDS = 24 * 3600
_cached_index: tuple[str, float] | None = None


async def latest_index(policy: EgressPolicy, *, timeout_seconds: float = 15.0) -> str | None:
    """The newest crawl's index id (e.g. `CC-MAIN-2026-33`), or `None`.

    Cached per process for a day. The index list changes monthly, so a stale
    value costs nothing worse than querying last month's crawl -- and refetching
    it per call would mean two requests for every map.
    """

    global _cached_index
    if _cached_index is not None:
        index_id, fetched_at = _cached_index
        if time.monotonic() - fetched_at < _INDEX_TTL_SECONDS:
            return index_id

    try:
        response = await guarded_get(COLLINFO_URL, policy, timeout=timeout_seconds)
        if response.status_code != 200:
            return None
        collections = response.json()
    except (EgressBlocked, httpx.HTTPError, ValueError, json.JSONDecodeError):
        log.warning("crawl.cc.collinfo_unavailable")
        return None

    if not isinstance(collections, list) or not collections:
        return None
    index_id = collections[0].get("id")
    if not isinstance(index_id, str) or not index_id:
        return None

    _cached_index = (index_id, time.monotonic())
    return index_id


async def fetch_urls(
    base_domain: str,
    policy: EgressPolicy,
    *,
    max_urls: int = 10_000,
    timeout_seconds: float = 30.0,
    include_subdomains: bool = True,
) -> list[str]:
    """Every URL the latest index holds for `base_domain`, capped at `max_urls`.

    Fail-open like every other source in this package: an unreachable index, a
    rate-limited response or a malformed line contributes nothing rather than
    aborting the caller's map.
    """

    index_id = await latest_index(policy, timeout_seconds=timeout_seconds)
    if index_id is None:
        return []

    glob = f"*.{base_domain}/*" if include_subdomains else f"{base_domain}/*"
    url = (
        f"{INDEX_URL_TEMPLATE.format(index_id=index_id)}"
        f"?url={quote(glob, safe='*')}&output=json"
    )

    try:
        response = await guarded_get(url, policy, timeout=timeout_seconds)
    except (EgressBlocked, httpx.HTTPError):
        log.warning("crawl.cc.query_failed", domain=base_domain)
        return []
    if response.status_code != 200:
        # 404 is the index's way of saying "nothing for this domain", which is an
        # answer rather than a failure. Anything else is worth a line in the log.
        if response.status_code != 404:
            log.warning(
                "crawl.cc.query_status", domain=base_domain, status=response.status_code
            )
        return []

    out: list[str] = []
    seen: set[str] = set()
    for line in response.text.splitlines():
        if len(out) >= max_urls:
            break
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # The index is JSONL; one bad line is not a reason to discard the
            # rest of a 40,000-line response.
            continue
        candidate = record.get("url")
        if isinstance(candidate, str) and candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def reset_index_cache() -> None:
    """Test seam. The module-level cache would otherwise leak a fetched index id
    between tests and make the second one pass for the wrong reason."""

    global _cached_index
    _cached_index = None
