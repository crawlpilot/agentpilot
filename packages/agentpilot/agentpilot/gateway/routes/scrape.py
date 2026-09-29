"""The real `/v1/scrape` implementation: a thin HTTP wrapper around
`crawlpilot.session.ephemeral.run_ephemeral_scrape` (the same function the
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
from agentpilot.jobs.cache import (
    CachePolicy,
    cache_key,
    is_cacheable,
    is_cacheable_result,
)
from agentpilot.llm.structured import extract_structured
from agentpilot.observability.metrics import requests_total, scrape_duration_seconds
from crawlpilot.session.ephemeral import run_ephemeral_scrape
from crawlpilot.spi.scrape import Document, ExtractConfig, ScrapeOptions

log = structlog.get_logger(__name__)

router = APIRouter(tags=["scrape"])


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _proxy_required_for_stealth() -> bool:
    """Whether `tier=stealth|enhanced` must refuse to run without a proxy pool.

    **Off by default.** The guard assumed the worker's own IP is a datacenter
    IP, which is only true of some deployments. MEASURED from a worker on a
    residential line with no proxy: cos.com (Akamai Bot Manager) served the
    product page 5/5 on the stealth tier. Failing closed there blocked the one
    configuration that works, and it was inconsistent besides -- `tier=auto`
    runs the very same stealth rung with no proxy and was always let through.

    A deployment whose workers egress from a cloud ASN should turn it on with
    `AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH=1`: there the IP really is the
    dominant block signal, and a 503 is more honest than a burned identity.

    `AGENTPILOT_ALLOW_PROXYLESS_STEALTH`, the old opt-*out*, is still honoured
    and wins, so an existing deployment that set it keeps its behaviour.

    Read at call time, not import time, so a test (or an operator toggling the
    env) does not need a process restart.
    """

    if _truthy("AGENTPILOT_ALLOW_PROXYLESS_STEALTH"):
        return False
    return _truthy("AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH")


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

    # Optionally fail closed rather than scrape from the raw worker IP when the
    # caller asked for a stealth-grade run -- see `_proxy_required_for_stealth`
    # for why that is opt-in. `basic`/`auto` are never gated.
    if req.tier in ("stealth", "enhanced") and wiring.proxy_pinner is None:
        if _proxy_required_for_stealth():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"tier={req.tier!r} requires a proxy pool on this deployment, but none "
                    "is configured (set AGENTPILOT_PROXY_POOL to a residential/mobile pool, "
                    "or unset AGENTPILOT_REQUIRE_PROXY_FOR_STEALTH to run this tier from "
                    "the worker's own IP)."
                ),
            )
        log.info("scrape.proxyless_stealth", tier=req.tier, domain=domain)

    requests_total.labels(tenant=req.tenant, route="scrape").inc()

    options = ScrapeOptions(
        formats=tuple(req.formats),
        only_main_content=req.only_main_content,
        include_tags=tuple(req.include_tags) or None,
        exclude_tags=tuple(req.exclude_tags) or None,
        relevance_query=req.relevance_query,
        citations=req.citations,
        timeout_ms=req.timeout_ms,
        wait_for_ms=req.wait_for_ms,
        actions=tuple(
            to_spi_action(a, wiring.extensions_for(req.extensions).tools) for a in req.actions
        ),
        screenshot=req.screenshot,
        full_page_screenshot=req.full_page_screenshot,
        extract=ExtractConfig(json_schema=req.extract.json_schema, prompt=req.extract.prompt)
        if req.extract
        else None,
    )

    # `variant` carries the request-level fields that change what the server
    # returns but do not live on `ScrapeOptions` -- without them a scrape asking
    # for `locale="ja-JP"` could be served the `en-US` page a previous caller
    # cached. `tier` is included even though every tier currently takes the same
    # path: it is *documented* as future routing, and a key that omits it would
    # start silently mixing tiers the moment that lands.
    policy = CachePolicy(
        mode=req.cache_mode,
        **({"max_age_ms": req.max_age_ms} if req.max_age_ms is not None else {}),
    )
    key = (
        cache_key(
            tenant=req.tenant,
            url=req.url,
            options=options,
            variant={
                "locale": req.locale,
                "timezone_id": req.timezone_id,
                "tier": req.tier,
                "extensions": sorted(req.extensions) if req.extensions is not None else None,
            },
        )
        if is_cacheable(options, session_name=req.session_name)
        else None
    )
    if key is not None and policy.may_read:
        cached = await wiring.scrape_cache.get(key, max_age_ms=policy.max_age_ms)
        if cached is not None:
            log.info("scrape.cache_hit", url=req.url, tenant=req.tenant)
            return ScrapeResponse(success=True, data=_document_out(cached, cached_hit=True))

    with scrape_duration_seconds.time():
        document, screenshot_bytes = await run_ephemeral_scrape(
            browser_config=wiring.browser_config,
            prototype_provider=wiring.prototype_provider,
            block_hooks=wiring.extensions_for(req.extensions).blocks,
            structured_extractor=extract_structured,
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

    if key is not None and policy.may_write and is_cacheable_result(document):
        # `is_cacheable_result`, not `document.error is None`: a scrape whose LLM
        # extraction failed returns a *successful* document with `extract_error`
        # set, and storing that serves the failure back for the whole TTL without
        # ever retrying the model. See that function's docstring.
        await wiring.scrape_cache.put(
            key, tenant=req.tenant, url=req.url, document=document
        )

    return ScrapeResponse(
        success=True,
        data=_document_out(
            document,
            screenshot_bytes=screenshot_bytes,
            fallback_tier=req.tier,
        ),
    )


def _document_out(
    document: Document,
    *,
    screenshot_bytes: bytes | None = None,
    cached_hit: bool = False,
    fallback_tier: str = "auto",
) -> DocumentOut:
    """One mapper for both paths, so a cache hit and a fresh scrape cannot drift
    into returning different shapes -- which is exactly the bug a caller would
    report as "the cache loses my entities"."""

    meta = document.metadata
    return DocumentOut(
        document_id=document.document_id,
        url=document.url,
        markdown=document.markdown,
        fit_markdown=document.fit_markdown,
        text=document.text,
        html=document.html,
        structured_data=document.structured_data,
        tables=document.tables,
        entities=document.entities,
        links=list(document.links),
        screenshot=base64.b64encode(screenshot_bytes).decode("ascii")
        if screenshot_bytes
        else None,
        metadata=ScrapeMetadataOut(
            title=meta.title if meta else None,
            status_code=meta.status_code if meta else None,
            tier_used=meta.tier_used if meta else fallback_tier,
            node_id=meta.node_id if meta else "",
            duration_ms=meta.duration_ms if meta else 0.0,
            source_url=document.url,
        ),
        cached=cached_hit,
        error=document.error,
        extract=document.extract,
        extract_error=document.extract_error,
        extract_warning=document.extract_warning,
    )
