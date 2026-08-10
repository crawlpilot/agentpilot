"""The crawl-processing loop: claims queued `crawl_tasks` rows, runs an
ephemeral scrape for each (via `agentpilot.session.ephemeral
.run_ephemeral_scrape`, the exact same composition `/v1/scrape` uses),
persists the result, and -- for `crawl` jobs -- expands the frontier with
newly discovered links. Folded into the existing `AGENTPILOT_ROLE=worker`
process (started in `gateway.wiring`'s `_init_worker()`, next to `Reaper`)
rather than a separate role, per an explicit P4 decision: every worker polls
the same Postgres `crawl_tasks` table via `PostgresJobStore
.claim_tasks_batch()`'s `FOR UPDATE SKIP LOCKED`, which already handles fair
distribution across however many worker processes are running -- no
placement/routing needed, unlike interactive session opens, since a claimed
task's ephemeral browser context is opened locally on whichever node
claimed it.

**Known simplifications**, both named rather than silently wrong:
- Concurrency is bounded globally per worker process (`max_concurrent`), not
  per-job via `CrawlOptions.max_concurrency` -- true per-job throttling would
  need claiming scoped to one job at a time or in-process per-job counters.
- `CrawlOptions.limit` is enforced as a soft cap during frontier expansion
  (see `JobForWorker.total`'s docstring), not an atomic guarantee under
  concurrent expansion from multiple claimed tasks of the same job.
- Webhook delivery (`agentpilot.webhook`, P6) doesn't exist yet -- a
  finalized job's `completed`/`failed` event is logged but not delivered.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from urllib.parse import urlparse

import structlog

from agentpilot.crawl.frontier import expand_frontier
from agentpilot.crawl.robots import fetch as fetch_robots
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.jobs.options_codec import load_batch_scrape_options, load_crawl_options
from agentpilot.jobs.store import ClaimedTask, JobForWorker, PostgresJobStore
from agentpilot.session.ephemeral import run_ephemeral_scrape
from agentpilot.session.registry import RegistryProtocol
from agentpilot.session.warm_pool import WarmPool
from agentpilot.spi.crawl import CrawlOptions
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.scrape import Document

log = structlog.get_logger(__name__)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


class CrawlWorkerLoop:
    def __init__(
        self,
        store: PostgresJobStore,
        registry: RegistryProtocol,
        driver: BrowserDriver,
        profiles_root: Path,
        proxy_pinner: ProxyPinner | None,
        *,
        lease_ttl_seconds: float = 300.0,
        batch_size: int = 10,
        max_concurrent: int = 5,
        poll_interval_seconds: float = 2.0,
        stale_after_seconds: float = 120.0,
        warm_pool: WarmPool | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._driver = driver
        self._profiles_root = profiles_root
        self._proxy_pinner = proxy_pinner
        self._warm_pool = warm_pool
        self._lease_ttl_seconds = lease_ttl_seconds
        self._batch_size = batch_size
        self._max_concurrent = max_concurrent
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("crawl_worker_loop.tick_failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self) -> None:
        await self._store.reclaim_stale_tasks(self._stale_after_seconds)
        claimed = await self._store.claim_tasks_batch(self._batch_size)
        if not claimed:
            return
        semaphore = asyncio.Semaphore(self._max_concurrent)
        await asyncio.gather(*(self._process(task, semaphore) for task in claimed))

    async def _process(self, task: ClaimedTask, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            job = await self._store.get_job_for_worker(task.job_id)
            if job is None:
                # Shouldn't happen -- crawl_tasks has an ON DELETE CASCADE FK
                # to jobs -- but fail this one task defensively rather than
                # crashing the whole tick's asyncio.gather over one bad row.
                await self._store.fail_task(task.task_id, task.lock, "job not found")
                return

            try:
                await self._process_task(task, job)
            except Exception as exc:
                log.warning(
                    "crawl_worker_loop.task_failed", task_id=task.task_id, error=str(exc)
                )
                await self._store.fail_task(task.task_id, task.lock, str(exc))
                return

            status = await self._store.try_finalize_job(job.job_id)
            if status is not None:
                log.info("crawl_worker_loop.job_finalized", job_id=job.job_id, status=status)

    async def _process_task(self, task: ClaimedTask, job: JobForWorker) -> None:
        crawl_options: CrawlOptions | None = None
        if job.job_type == "crawl":
            crawl_options = load_crawl_options(job.options)
            scrape_options = crawl_options.scrape_options
        else:
            scrape_options = load_batch_scrape_options(job.options).scrape_options

        domain = urlparse(task.url).hostname
        if not domain:
            await self._store.fail_task(
                task.task_id, task.lock, f"cannot determine domain from url {task.url!r}"
            )
            return

        document, _screenshot = await run_ephemeral_scrape(
            tenant=job.tenant,
            domain=domain,
            url=task.url,
            options=scrape_options,
            registry=self._registry,
            driver=self._driver,
            profiles_root=self._profiles_root,
            proxy_pinner=self._proxy_pinner,
            lease_ttl_seconds=self._lease_ttl_seconds,
            warm_pool=self._warm_pool,
        )

        if document.markdown is None and document.html is None and document.text is None:
            # Nothing usable came back at all -- treat as a task failure
            # (eligible for retry) rather than persisting an empty document.
            await self._store.fail_task(
                task.task_id, task.lock, document.error or "scrape produced no content"
            )
            return

        # Frontier expansion (which bumps jobs.total, via enqueue_tasks)
        # *before* complete_task (which bumps jobs.completed) -- not the
        # other way around. This task's own newly-discovered links must
        # already be counted in `total` by the time this task's completion
        # is counted, or a concurrent sibling task's try_finalize_job() could
        # observe completed+failed >= total (this task done, its siblings
        # also done, this task's *own* discoveries not yet enqueued) and
        # finalize the job prematurely, before those new tasks ever exist.
        # A failure here must not discard an otherwise-good scrape result,
        # so it's caught and logged rather than propagated into the
        # `except Exception` in `_process()` that would fail_task() this
        # (successful!) scrape.
        if crawl_options is not None:
            try:
                await self._expand_frontier(task, job, crawl_options, document)
            except Exception:
                log.warning(
                    "crawl_worker_loop.frontier_expansion_failed", task_id=task.task_id
                )

        await self._store.complete_task(task.task_id, task.lock, document)

    async def _expand_frontier(
        self, task: ClaimedTask, job: JobForWorker, options: CrawlOptions, document: Document
    ) -> None:
        if document.html is None:
            # Frontier expansion needs raw HTML to find links in -- a crawl
            # whose scrape_options never requested `formats: ["html"]` stays
            # single-page-per-task (no further discovery from this page), a
            # documented limitation rather than a silent no-op mystery.
            return
        if options.max_discovery_depth is not None and task.depth >= options.max_discovery_depth:
            return
        remaining = max(options.limit - job.total, 0)
        if remaining == 0:
            return

        seed_url = job.url or options.url
        robots_parser = None
        if not options.ignore_robots_txt:
            robots_parser = await fetch_robots(_origin(seed_url), EgressPolicy())

        next_urls = expand_frontier(
            html=document.html,
            page_url=task.url,
            depth=task.depth,
            seed_url=seed_url,
            options=options,
            robots_parser=robots_parser,
        )
        if next_urls:
            await self._store.enqueue_tasks(
                job.job_id, next_urls[:remaining], depth=task.depth + 1
            )
