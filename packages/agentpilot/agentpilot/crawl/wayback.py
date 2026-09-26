"""The Wayback Machine's CDX API as a discovery source.

Complementary to Common Crawl rather than redundant with it. Common Crawl is a
broad sweep at a moment in time; the Internet Archive has been recording some
sites since 1996, so it holds URL shapes a site abandoned years ago alongside
ones it still serves. For mapping a large or long-lived domain this is often the
richest single source.

It is also the noisiest. Archived URLs include tracking parameters as they were
crawled, session ids, pages that 404'd at capture time, and genuinely malformed
strings -- the encoded-newline URLs `nonsense.py` filters exist because this
source returns them. Everything from here goes through normalization, the
nonsense filter, and soft-404 detection before a caller sees it.

Ported from crawl4ai's `DomainMapper._discover_via_wayback` (Apache-2.0;
crawl4ai 0.9.4). One difference: `collapse=urlkey` plus a `from` year bound, so a
domain with twenty years of history returns its recent shape rather than its 2004
shape padded out to the row limit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import structlog

from crawlpilot.egress.httpx_guard import guarded_get
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

log = structlog.get_logger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"

DEFAULT_YEARS_BACK = 3
"""How far back to ask. Deliberately not "everything": a URL last seen in 2009
is almost certainly gone, and including it spends the row limit on results that
will fail liveness anyway. Three years keeps the long tail a site still serves
without dredging up its previous two redesigns."""


async def fetch_urls(
    base_domain: str,
    policy: EgressPolicy,
    *,
    max_urls: int = 10_000,
    timeout_seconds: float = 30.0,
    include_subdomains: bool = True,
    years_back: int = DEFAULT_YEARS_BACK,
) -> list[str]:
    """URLs the archive has recorded for `base_domain`, newest shape first.

    Fail-open: the CDX API is frequently slow and periodically rate-limits. A
    timeout here contributes nothing and does not fail the caller's map, which is
    why `sources.gather` also gives every source its own deadline.
    """

    pattern = f"*.{base_domain}/*" if include_subdomains else f"{base_domain}/*"
    params = {
        "url": pattern,
        "output": "text",
        "fl": "original",
        # One row per distinct URL rather than one per capture -- without this a
        # daily-archived homepage alone fills the limit.
        "collapse": "urlkey",
        "filter": "statuscode:200",
        "from": str(datetime.now(UTC).year - years_back),
        "limit": str(max_urls),
    }

    try:
        response = await guarded_get(
            CDX_URL, policy, params=params, timeout=timeout_seconds, follow_redirects=True
        )
    except (EgressBlocked, httpx.HTTPError):
        log.warning("crawl.wayback.query_failed", domain=base_domain)
        return []
    if response.status_code != 200:
        log.warning(
            "crawl.wayback.query_status", domain=base_domain, status=response.status_code
        )
        return []

    out: list[str] = []
    seen: set[str] = set()
    for line in response.text.splitlines():
        if len(out) >= max_urls:
            break
        candidate = line.strip()
        if not candidate or candidate in seen:
            continue
        # The archive holds URLs for hosts that merely *mention* the pattern in a
        # redirect chain, so the host is re-checked here rather than trusted.
        if not _is_in_domain(candidate, base_domain, include_subdomains):
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def _is_in_domain(url: str, base_domain: str, include_subdomains: bool) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == base_domain:
        return True
    return include_subdomains and host.endswith(f".{base_domain}")


async def fetch_hosts(
    base_domain: str,
    policy: EgressPolicy,
    *,
    max_urls: int = 10_000,
    timeout_seconds: float = 30.0,
) -> set[str]:
    """The distinct hosts appearing in the archive for this domain.

    A separate use of the same query: for `/v1/map` the URLs matter, but for
    subdomain discovery only the hostnames do, and the archive knows about
    subdomains no certificate ever covered.
    """

    urls = await fetch_urls(
        base_domain,
        policy,
        max_urls=max_urls,
        timeout_seconds=timeout_seconds,
        include_subdomains=True,
    )
    hosts: set[str] = set()
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if host and (host == base_domain or host.endswith(f".{base_domain}")):
            hosts.add(host)
    return hosts
