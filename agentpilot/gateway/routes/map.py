"""`POST /v1/map` -- fast URL discovery, synchronous, no job queue. Mounted
identically on both `monolith` and `gateway` (no `_proxy` variant, no
`/internal/...` counterpart): `agentpilot.crawl` is pure guarded-HTTP
discovery, no browser/driver involved, so a `gateway` process can serve this
directly out of its own (Chrome-free) process without a worker hop, exactly
like `routes/api_keys.py`/`routes/nodes.py` need no proxy variant. Each
route declares `Depends(require_tenant_auth)` itself (rather than at
`include_router()` time) since that's the only way to actually receive the
resolved `AuthedTenant` in the handler -- same per-route idiom
`routes/gateway_proxy.py` uses.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException

from agentpilot.auth.models import AuthedTenant
from agentpilot.crawl.seed import discover_for_map
from agentpilot.egress.httpx_guard import guarded_get
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.schemas import MapLinkOut, MapRequest, MapResponse
from agentpilot.observability.metrics import requests_total
from agentpilot.spi.crawl import MapOptions
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import EgressBlocked

router = APIRouter(tags=["map"])


def _registrable_domain(host: str) -> str:
    # Same last-two-labels heuristic `crawl.filters` uses -- good enough for
    # the base-domain hint and domain-change redirect check without pulling in
    # a public-suffix list (a known, deliberately-deferred miss for multi-part
    # TLDs like `.co.uk`).
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_base_domain(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host == _registrable_domain(host) and (parsed.path or "/") in ("", "/")


async def _resolve_seed_url(url: str, policy: EgressPolicy) -> str:
    """Follow redirects once and, only if they land on a *different*
    registrable domain, adopt the final URL -- mirrors Firecrawl's
    `resolveRedirects` + `!isSameDomain` hostname swap (so a link-shortener or
    apex→other-domain redirect maps the real destination). Fail-open: any
    fetch error leaves the original URL untouched."""

    try:
        resp = await guarded_get(url, policy, timeout=15.0, follow_redirects=True)
    except (EgressBlocked, httpx.HTTPError):
        return url
    final = str(resp.url)
    orig_host = urlparse(url).hostname or ""
    final_host = urlparse(final).hostname or ""
    if final_host and _registrable_domain(final_host) != _registrable_domain(orig_host):
        return final
    return url


@router.post("", response_model=MapResponse)
async def map_urls(
    req: MapRequest, authed: AuthedTenant = Depends(require_tenant_auth)
) -> MapResponse:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="map").inc()

    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"url must be http(s): {req.url!r}")

    policy = EgressPolicy()
    seed_url = await _resolve_seed_url(req.url, policy)

    options = MapOptions(
        url=seed_url,
        include_paths=tuple(req.include_paths),
        exclude_paths=tuple(req.exclude_paths),
        sitemap=req.sitemap,
        include_subdomains=req.include_subdomains,
        ignore_query_parameters=req.ignore_query_parameters,
        limit=req.limit,
        search=req.search,
        allow_external_links=req.allow_external_links,
        filter_by_path=req.filter_by_path,
        max_discovery_depth=req.max_discovery_depth,
        timeout_ms=req.timeout,
    )

    try:
        if req.timeout is not None:
            links = await asyncio.wait_for(
                discover_for_map(options, policy), req.timeout / 1000
            )
        else:
            links = await discover_for_map(options, policy)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="map discovery timed out") from None

    # Firecrawl's "did you mean the base domain?" hint: a non-root seed that
    # yields ≤1 link often means the caller wanted the whole site.
    warning: str | None = None
    if len(links) <= 1 and req.limit != 1 and not _is_base_domain(seed_url):
        parsed = urlparse(seed_url)
        base = f"{parsed.scheme}://{_registrable_domain(parsed.hostname or '')}"
        warning = (
            f"Only {len(links)} result(s) found. "
            f"For broader coverage, try mapping the base domain: {base}"
        )

    return MapResponse(
        success=True,
        links=[
            MapLinkOut(url=link.url, title=link.title, description=link.description)
            for link in links
        ],
        warning=warning,
    )
