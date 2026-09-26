"""Guessing: DNS for subdomains, conventional paths for pages.

The weakest of the discovery sources and the only one that works on a site with
no sitemap, no feed, no certificate history and nothing in any public index --
which describes most small sites. Two kinds of guess:

* **Subdomain resolution.** Take a list of conventional prefixes (`www`, `api`,
  `blog`, `shop`, `docs`) plus whatever `crt.py` found, and ask DNS which of them
  exist. Cheap, and it is also how a certificate-derived candidate gets confirmed
  rather than trusted.
* **Path probing.** Request a list of conventional paths (`/about`, `/pricing`,
  `/blog`, `/contact`) and keep the ones that answer with real content. This is
  where `soft404` earns its place: on an SPA every one of these returns 200, and
  without the fingerprint the probe "finds" every path it tried.

Ported from crawl4ai's `DomainMapper._guess_subdomains` / `_probe_paths`
(Apache-2.0; crawl4ai 0.9.4). Differences: DNS resolution is bounded by a
semaphore as well as a timeout (a 500-name certificate list otherwise opens 500
concurrent resolutions), and path probing runs the soft-404 check rather than
trusting a 200.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import structlog

from agentpilot.crawl import soft404
from crawlpilot.egress.httpx_guard import guarded_get
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

log = structlog.get_logger(__name__)

COMMON_SUBDOMAINS = (
    "www", "api", "app", "blog", "docs", "shop", "store", "help", "support",
    "news", "developer", "developers", "status", "m", "mobile", "cdn", "static",
    "assets", "media", "images", "img", "admin", "portal", "account", "accounts",
    "login", "auth", "dashboard", "console", "staging", "dev", "test", "beta",
    "demo", "careers", "jobs", "about", "press", "investors", "legal", "partners",
    "community", "forum", "events", "learn", "academy", "training",
)  # fmt: skip

COMMON_PATHS = (
    "/about", "/about-us", "/company", "/contact", "/contact-us", "/pricing",
    "/plans", "/products", "/services", "/solutions", "/features", "/blog",
    "/news", "/press", "/careers", "/jobs", "/docs", "/documentation", "/help",
    "/support", "/faq", "/terms", "/privacy", "/legal", "/sitemap", "/search",
    "/login", "/signup", "/register", "/team", "/partners", "/customers",
    "/case-studies", "/resources", "/downloads", "/api", "/developers",
    "/changelog", "/status", "/security",
)  # fmt: skip

DNS_CONCURRENCY = 32
PROBE_CONCURRENCY = 8
"""Probing is HTTP against one host, so this is per-host concurrency and stays
low. DNS goes to a resolver, which is built for volume."""


async def resolve_hosts(
    candidates: set[str],
    *,
    timeout_seconds: float = 3.0,
    concurrency: int = DNS_CONCURRENCY,
) -> set[str]:
    """Which of `candidates` resolve. Nothing is fetched here.

    This is the step that separates a certificate log's wishful thinking from
    hosts that exist: `crt.py` returns every name ever put on a certificate,
    including internal ones that were never public and ones decommissioned years
    ago.
    """

    if not candidates:
        return set()

    semaphore = asyncio.Semaphore(max(concurrency, 1))
    loop = asyncio.get_running_loop()

    async def resolves(host: str) -> str | None:
        async with semaphore:
            try:
                await asyncio.wait_for(
                    loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
                    timeout=timeout_seconds,
                )
            except (TimeoutError, OSError, UnicodeError):
                return None
            return host

    results = await asyncio.gather(*(resolves(host) for host in sorted(candidates)))
    return {host for host in results if host is not None}


def subdomain_candidates(base_domain: str, *, extra: tuple[str, ...] = ()) -> set[str]:
    prefixes = (*COMMON_SUBDOMAINS, *extra)
    return {f"{prefix}.{base_domain}" for prefix in prefixes}


async def probe_paths(
    origin: str,
    policy: EgressPolicy,
    *,
    paths: tuple[str, ...] = COMMON_PATHS,
    timeout_seconds: float = 10.0,
    concurrency: int = PROBE_CONCURRENCY,
    fingerprint: soft404.Soft404Fingerprint | None = None,
) -> list[str]:
    """Conventional paths on `origin` that answer with a real page.

    `fingerprint` should come from `soft404.fingerprint(origin, ...)`. Without it
    every 200 is taken at face value, which on a single-page app means every path
    in the list is reported as found -- the probe's results become a copy of its
    input, which is worse than returning nothing because it looks like data.
    """

    semaphore = asyncio.Semaphore(max(concurrency, 1))
    base = origin.rstrip("/")

    async def probe(path: str) -> str | None:
        url = f"{base}{path}"
        async with semaphore:
            try:
                response = await guarded_get(
                    url, policy, timeout=timeout_seconds, follow_redirects=True
                )
            except (EgressBlocked, httpx.HTTPError):
                return None

        if response.status_code >= 400:
            return None
        body = response.content[:4096]
        final_path = httpx.URL(str(response.url)).path or "/"
        if soft404.is_soft_404(
            status_code=response.status_code,
            body=body,
            final_path=final_path,
            fingerprint=fingerprint,
        ):
            return None
        # Report where the request landed, not where it was aimed: a probe of
        # `/about` that redirects to `/company/about` has found the latter, and
        # returning the former would hand the caller a URL that redirects.
        return str(response.url)

    results = await asyncio.gather(*(probe(path) for path in paths))

    out: list[str] = []
    seen: set[str] = set()
    for url in results:
        if url is not None and url not in seen:
            seen.add(url)
            out.append(url)
    return out
