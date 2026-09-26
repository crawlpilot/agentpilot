"""Certificate Transparency as a subdomain source.

Every publicly-trusted TLS certificate issued since 2018 is logged to append-only
public logs, and each certificate names the hosts it covers. So the logs are, as a
side effect, a near-complete list of every subdomain a site has ever put behind
HTTPS -- including the ones it never linked to. `api.`, `staging.`, `legacy.`,
`shop.eu.`: all of them show up here and none of them show up in a sitemap.

This is the source that turns "map example.com" from "map www.example.com" into
something that actually covers the domain. crt.sh is the searchable front end.

Ported from crawl4ai's `DomainMapper._discover_via_crt` (Apache-2.0; crawl4ai
0.9.4). Three differences:

1. **Wildcards yield the parent, not a literal host.** Upstream strips `*.` and
   keeps the remainder, which turns a `*.example.com` certificate into the host
   `example.com` — fine — but a `*.eu.example.com` certificate into
   `eu.example.com`, which may not resolve at all. Both are now emitted as
   candidates and validated by DNS in `probe.py` rather than trusted.
2. **Expired certificates are excluded.** A host whose certificate lapsed three
   years ago is usually gone, and including it spends the probe budget on
   failures.
3. **A real timeout and a bounded result.** crt.sh is slow on large domains --
   tens of seconds is normal and it sometimes just hangs. Being one source among
   several, it gets a deadline and loses nothing but itself when it misses.
"""

from __future__ import annotations

import json

import httpx
import structlog

from crawlpilot.egress.httpx_guard import guarded_get
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.errors import EgressBlocked

log = structlog.get_logger(__name__)

CRT_SH_URL = "https://crt.sh/"

MAX_HOSTS = 500
"""A large domain can have thousands of logged hostnames, most of them dead
internal names. Every one costs a DNS lookup in `probe.py` and potentially a
scan, so the list is capped -- and because entries come back newest-first, the cap
keeps the current ones."""


async def fetch_hosts(
    base_domain: str,
    policy: EgressPolicy,
    *,
    timeout_seconds: float = 25.0,
    max_hosts: int = MAX_HOSTS,
    exclude_expired: bool = True,
) -> set[str]:
    """Hostnames under `base_domain` named by any logged certificate.

    Candidates, not confirmed hosts: a certificate proves a name was requested,
    not that it resolves today. `probe.py` does the DNS validation.
    """

    params = {"q": f"%.{base_domain}", "output": "json"}
    if exclude_expired:
        params["exclude"] = "expired"

    try:
        response = await guarded_get(
            CRT_SH_URL, policy, params=params, timeout=timeout_seconds, follow_redirects=True
        )
    except (EgressBlocked, httpx.HTTPError):
        log.warning("crawl.crt.query_failed", domain=base_domain)
        return set()
    if response.status_code != 200:
        log.warning("crawl.crt.query_status", domain=base_domain, status=response.status_code)
        return set()

    try:
        entries = response.json()
    except (ValueError, json.JSONDecodeError):
        # crt.sh answers with an HTML error page under load, with a 200.
        log.warning("crawl.crt.malformed_response", domain=base_domain)
        return set()
    if not isinstance(entries, list):
        return set()

    hosts: set[str] = set()
    for entry in entries:
        if len(hosts) >= max_hosts:
            break
        if not isinstance(entry, dict):
            continue
        for field in ("common_name", "name_value"):
            raw = entry.get(field)
            if not isinstance(raw, str):
                continue
            # `name_value` packs every SAN on the certificate into one
            # newline-separated string.
            for name in raw.split("\n"):
                host = _clean(name)
                if host and _in_domain(host, base_domain):
                    hosts.add(host)
    return hosts


def _clean(name: str) -> str | None:
    host = name.strip().lower().rstrip(".")
    if host.startswith("*."):
        # A wildcard names its parent as a candidate. `*.example.com` gives
        # `example.com`; `*.eu.example.com` gives `eu.example.com`, which may or
        # may not resolve -- that is DNS's question to answer, not ours.
        host = host[2:]
    if not host or "*" in host or " " in host:
        return None
    return host


def _in_domain(host: str, base_domain: str) -> bool:
    return host == base_domain or host.endswith(f".{base_domain}")
