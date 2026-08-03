"""`/v1/crawl` -- async, Postgres-queue-backed site crawl. `POST` creates and
seeds a job (the seed URL itself at depth 0, everything discovered alongside
it -- sitemap + the seed page's own links -- one level deeper at depth 1;
this split is load-bearing, see the comment at the enqueue call below and
`agentpilot.jobs.worker_loop`'s own tests, which caught the bug of enqueueing
the whole batch at depth 0), `GET /{id}` polls status + paginated results,
`DELETE /{id}` cancels. Mounted identically on both `monolith` and `gateway`
(no `_proxy` variant): job CRUD never touches `agentpilot.driver`, exactly
like `routes/api_keys.py` needs none either. The actual crawl *processing*
happens in `agentpilot.jobs.worker_loop.CrawlWorkerLoop`, running
independently on every worker/monolith process -- this route only
creates/reads/cancels rows in `agentpilot.jobs.store.PostgresJobStore`.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from agentpilot.auth.models import AuthedTenant
from agentpilot.crawl.seed import discover_for_crawl
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.schemas import (
    CrawlCreateResponse,
    CrawlRequest,
    CrawlStatusResponse,
    DocumentOut,
    ScrapeMetadataOut,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.jobs.options_codec import dump_crawl_options
from agentpilot.jobs.store import PostgresJobStore
from agentpilot.observability.metrics import requests_total
from agentpilot.spi.crawl import CrawlOptions
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import JobNotFound
from agentpilot.spi.scrape import Document, ScrapeOptions
from agentpilot.spi.webhook import WebhookConfig

log = structlog.get_logger(__name__)

router = APIRouter(tags=["crawl"])


def _require_jobs_store(wiring: Wiring) -> PostgresJobStore:
    if wiring.jobs_store is None:
        raise HTTPException(
            status_code=503, detail="crawl requires AGENTPILOT_DATABASE_URL to be configured"
        )
    return wiring.jobs_store


def _to_crawl_options(req: CrawlRequest) -> CrawlOptions:
    return CrawlOptions(
        url=req.url,
        include_paths=tuple(req.include_paths),
        exclude_paths=tuple(req.exclude_paths),
        max_discovery_depth=req.max_discovery_depth,
        limit=req.limit,
        allow_external_links=req.allow_external_links,
        allow_subdomains=req.allow_subdomains,
        allow_backward_crawling=req.allow_backward_crawling,
        ignore_robots_txt=req.ignore_robots_txt,
        sitemap=req.sitemap,
        deduplicate_similar_urls=req.deduplicate_similar_urls,
        ignore_query_parameters=req.ignore_query_parameters,
        delay_ms=req.delay_ms,
        max_concurrency=req.max_concurrency,
        scrape_options=ScrapeOptions(
            formats=tuple(req.scrape_options.formats),
            only_main_content=req.scrape_options.only_main_content,
            timeout_ms=req.scrape_options.timeout_ms,
            wait_for_ms=req.scrape_options.wait_for_ms,
            screenshot=req.scrape_options.screenshot,
            full_page_screenshot=req.scrape_options.full_page_screenshot,
        ),
    )


def _document_out(document: Document) -> DocumentOut:
    meta = document.metadata
    return DocumentOut(
        document_id=document.document_id,
        url=document.url,
        markdown=document.markdown,
        text=document.text,
        html=document.html,
        links=list(document.links),
        # Not persisted inline for job-backed results -- see
        # spi.scrape.Document.screenshot_artifact_id's docstring.
        screenshot=None,
        metadata=ScrapeMetadataOut(
            title=meta.title,
            status_code=meta.status_code,
            tier_used=meta.tier_used,
            node_id=meta.node_id,
            duration_ms=meta.duration_ms,
            source_url=document.url,
        )
        if meta is not None
        else None,
        error=document.error,
    )


@router.post("", response_model=CrawlCreateResponse)
async def create_crawl(
    req: CrawlRequest,
    request: Request,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> CrawlCreateResponse:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="create_crawl").inc()
    store = _require_jobs_store(wiring)

    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"url must be http(s): {req.url!r}")

    options = _to_crawl_options(req)
    webhook_secret = uuid.uuid4().hex if req.webhook else None
    webhook = (
        WebhookConfig(
            url=req.webhook.url,
            headers=req.webhook.headers,
            events=tuple(req.webhook.events),
            secret=webhook_secret or "",
        )
        if req.webhook
        else None
    )

    job = await store.create_job(
        req.tenant, "crawl", req.url, dump_crawl_options(options), webhook
    )

    result = await discover_for_crawl(options, EgressPolicy())
    # CrawlSeedResult.urls[0] is always the seed URL itself (depth 0, per
    # that dataclass's own docstring); everything else discovered alongside
    # it -- sitemap + the seed page's own links -- is one level deeper.
    # Enqueueing the *whole* batch at depth=0 would let those depth-1 pages'
    # own frontier expansion run one hop too early under
    # max_discovery_depth.
    await store.enqueue_tasks(job.job_id, result.urls[:1], depth=0)
    if len(result.urls) > 1:
        await store.enqueue_tasks(job.job_id, result.urls[1:], depth=1)
    await store.mark_started(job.job_id)
    # discovery_done is set once this *initial* seed is enqueued, not once
    # the frontier is fully exhausted -- see worker_loop.py's module
    # docstring for why the completed+failed>=total invariant already
    # blocks premature finalization while a claimed task's own expansion is
    # still pending.
    await store.mark_discovery_done(job.job_id)

    return CrawlCreateResponse(
        success=True,
        id=job.job_id,
        url=f"{request.base_url}v1/crawl/{job.job_id}",
        webhook_secret=webhook_secret,
    )


@router.get("/{job_id}", response_model=CrawlStatusResponse)
async def get_crawl(
    job_id: str,
    after: str | None = None,
    limit: int = 100,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> CrawlStatusResponse:
    store = _require_jobs_store(wiring)
    job = await store.get_job(job_id, authed.tenant)
    if job is None:
        raise JobNotFound(job_id)
    docs, next_cursor = await store.list_documents(job_id, authed.tenant, after, limit)
    return CrawlStatusResponse(
        success=True,
        status=job.status,
        total=job.total,
        completed=job.completed,
        failed=job.failed,
        data=[_document_out(d) for d in docs],
        next=next_cursor,
    )


@router.delete("/{job_id}")
async def cancel_crawl(
    job_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> dict[str, bool]:
    store = _require_jobs_store(wiring)
    ok = await store.cancel_job(job_id, authed.tenant)
    return {"success": ok}
