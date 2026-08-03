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

from fastapi import APIRouter, Depends, HTTPException

from agentpilot.auth.models import AuthedTenant
from agentpilot.crawl.seed import discover_for_map
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.schemas import MapLinkOut, MapRequest, MapResponse
from agentpilot.observability.metrics import requests_total
from agentpilot.spi.crawl import MapOptions
from agentpilot.spi.egress import EgressPolicy

router = APIRouter(tags=["map"])


@router.post("", response_model=MapResponse)
async def map_urls(
    req: MapRequest, authed: AuthedTenant = Depends(require_tenant_auth)
) -> MapResponse:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="map").inc()

    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"url must be http(s): {req.url!r}")

    options = MapOptions(
        url=req.url,
        include_paths=tuple(req.include_paths),
        exclude_paths=tuple(req.exclude_paths),
        sitemap=req.sitemap,
        include_subdomains=req.include_subdomains,
        ignore_query_parameters=req.ignore_query_parameters,
        limit=req.limit,
    )
    links = await discover_for_map(options, EgressPolicy())
    return MapResponse(
        success=True,
        links=[
            MapLinkOut(url=link.url, title=link.title, description=link.description)
            for link in links
        ],
    )
