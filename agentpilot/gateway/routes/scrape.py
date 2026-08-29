"""The real `/v1/scrape` implementation: a thin HTTP wrapper around
`agentpilot.session.ephemeral.run_ephemeral_scrape` (the same function the
crawl-worker loop uses per-task, `agentpilot.jobs.worker_loop`), converting
`ScrapeRequest` into `spi.scrape.ScrapeOptions` and the result into
`ScrapeResponse`. No baked-in path prefix -- `app.py` mounts this router only
on the `worker` role, at `/internal/scrape`. A `gateway`-role process never
mounts this; it mounts `scrape_proxy.py` at `/v1/scrape` instead, proxying to
a worker's `/internal/scrape`.
"""

from __future__ import annotations

import base64
import os
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
from agentpilot.spi.scrape import ExtractConfig, ScrapeOptions

log = structlog.get_logger(__name__)

router = APIRouter(tags=["scrape"])


def _proxyless_stealth_allowed() -> bool:
    """Opt out of the proxy fail-closed guard above.

    Read at call time, not import time, so a test (or an operator toggling the
    env) does not need a process restart -- and so the default stays "off"
    without a module-level constant that a test would have to monkeypatch.
    """

    return os.environ.get("AGENTPILOT_ALLOW_PROXYLESS_STEALTH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


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

    # Fail closed rather than silently scrape from the raw container IP when
    # the caller explicitly asked for a stealth-grade run: a datacenter
    # egress IP is the dominant Akamai-block signal on hardened targets, and
    # `tier=stealth|enhanced` with no proxy pool configured would otherwise
    # look like it's doing something it isn't. `basic`/`auto` stay lenient.
    #
    # The guard defends against *accidental* raw-IP scraping, not deliberate
    # raw-IP scraping. Without the opt-out below, a proxy-less deployment
    # cannot select the tiers that carry the stealth machinery at all, which
    # makes that machinery untestable and unreachable -- see
    # `session/stealth_profile.py`.
    if (
        req.tier in ("stealth", "enhanced")
        and wiring.proxy_pinner is None
        and not _proxyless_stealth_allowed()
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                f"tier={req.tier!r} requires a proxy pool, but none is configured "
                "(set AGENTPILOT_PROXY_POOL to a residential/mobile pool). "
                "Scraping bot-protected sites from the raw host IP will be blocked; "
                "use tier='basic' to proceed without a proxy anyway, or set "
                "AGENTPILOT_ALLOW_PROXYLESS_STEALTH=1 to run this tier from the "
                "host IP deliberately."
            ),
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
        extract=ExtractConfig(json_schema=req.extract.json_schema, prompt=req.extract.prompt)
        if req.extract
        else None,
    )

    with scrape_duration_seconds.time():
        document, screenshot_bytes = await run_ephemeral_scrape(
            browser_config=wiring.browser_config,
            prototype_provider=wiring.prototype_provider,
            scope=req.tenant,
            domain=domain,
            url=req.url,
            options=options,
            registry=wiring.registry,
            driver=wiring.driver,
            profiles_root=wiring.profiles_root,
            proxy_pinner=wiring.proxy_pinner,
            lease_ttl_seconds=wiring.lease_ttl_seconds,
            tier=req.tier,
            session_name=req.session_name,
            locale=req.locale,
            timezone_id=req.timezone_id,
            warm_pool=wiring.warm_pool,
            burn_tracker=wiring.burn_tracker,
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
            structured_data=document.structured_data,
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
            extract=document.extract,
            extract_error=document.extract_error,
            extract_warning=document.extract_warning,
        ),
    )
