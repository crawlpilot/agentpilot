"""The real `/v1/scrape` implementation: a thin HTTP wrapper around
`agentpilot.session.ephemeral.run_ephemeral_scrape` (the same function the
crawl-worker loop uses per-task, `agentpilot.jobs.worker_loop`), converting
`ScrapeRequest` into `spi.scrape.ScrapeOptions` and the result into
`ScrapeResponse`. No baked-in path prefix -- `app.py` mounts this same
router at `/internal/scrape` (monolith/worker) and `/v1/scrape` (monolith,
tenant-auth-gated), the same dual-mount idiom `routes/sessions.py` uses. A
`gateway`-role process never mounts this; it mounts `scrape_proxy.py` at
`/v1/scrape` instead, proxying to a worker's `/internal/scrape`.
"""

from __future__ import annotations

import base64
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from agentpilot.gateway.action_conversion import to_spi_action
from agentpilot.gateway.auth_deps import optional_authed_tenant
from agentpilot.gateway.schemas import (
    DocumentOut,
    ScrapeMetadataOut,
    ScrapeRequest,
    ScrapeResponse,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.observability.metrics import requests_total, scrape_duration_seconds
from agentpilot.session.ephemeral import run_ephemeral_scrape
from agentpilot.spi.scrape import ScrapeOptions

log = structlog.get_logger(__name__)

router = APIRouter(tags=["scrape"])


@router.post("", response_model=ScrapeResponse)
async def scrape(
    req: ScrapeRequest, request: Request, wiring: Wiring = Depends(get_wiring)
) -> ScrapeResponse:
    # Same tenant-mismatch dance as routes/sessions.py's open_session --
    # `authed` is non-None only on the gated `/v1/scrape` mount.
    authed = await optional_authed_tenant(request, wiring)
    if authed is not None and authed.tenant != req.tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch: api key does not own tenant")

    domain = urlparse(req.url).hostname
    if not domain:
        raise HTTPException(
            status_code=400, detail=f"cannot determine a domain from url {req.url!r}"
        )

    requests_total.labels(tenant=req.tenant, route="scrape").inc()

    options = ScrapeOptions(
        formats=tuple(req.formats),
        only_main_content=req.only_main_content,
        include_tags=tuple(req.include_tags) or None,
        exclude_tags=tuple(req.exclude_tags) or None,
        timeout_ms=req.timeout_ms,
        wait_for_ms=req.wait_for_ms,
        actions=tuple(to_spi_action(a) for a in req.actions),
        screenshot=req.screenshot,
        full_page_screenshot=req.full_page_screenshot,
    )

    with scrape_duration_seconds.time():
        document, screenshot_bytes = await run_ephemeral_scrape(
            tenant=req.tenant,
            domain=domain,
            url=req.url,
            options=options,
            registry=wiring.registry,
            driver=wiring.driver,
            profiles_root=wiring.profiles_root,
            proxy_pinner=wiring.proxy_pinner,
            lease_ttl_seconds=wiring.lease_ttl_seconds,
            tier=req.tier,
        )

    meta = document.metadata
    return ScrapeResponse(
        success=True,
        data=DocumentOut(
            document_id=document.document_id,
            url=document.url,
            markdown=document.markdown,
            text=document.text,
            html=document.html,
            links=list(document.links),
            screenshot=base64.b64encode(screenshot_bytes).decode("ascii")
            if screenshot_bytes
            else None,
            metadata=ScrapeMetadataOut(
                title=meta.title if meta else None,
                status_code=meta.status_code if meta else None,
                tier_used=meta.tier_used if meta else req.tier,
                node_id=meta.node_id if meta else "",
                duration_ms=meta.duration_ms if meta else 0.0,
                source_url=document.url,
            ),
            error=document.error,
        ),
    )
