"""The crawl-processing loop: claims queued `crawl_tasks` rows, runs an
ephemeral scrape for each (via `crawlpilot.session.ephemeral
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

Pacing and admission are handled by two collaborators rather than inline here:
`jobs.limiter` decides how long to wait before the next request to a given host
(honouring `CrawlOptions.delay_ms` and robots.txt `Crawl-delay`, and backing off
when a host starts returning 429/503), and `jobs.dispatch` decides how many
tasks this batch may admit given how much memory is actually left.
`jobs.cache` is consulted before each scrape, so an unchanged page does not open
a browser twice.

**Known simplifications**, both named rather than silently wrong:
- Concurrency is bounded globally per worker process (`max_concurrent`, further
  reduced under memory pressure by `jobs.dispatch`), not per-job via
  `CrawlOptions.max_concurrency` -- true per-job throttling would need claiming
  scoped to one job at a time or in-process per-job counters.
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
from urllib.robotparser import RobotFileParser

import structlog

from agentpilot.crawl.frontier import expand_frontier
from agentpilot.crawl.robots import crawl_delay as robots_crawl_delay
from agentpilot.crawl.robots import fetch as fetch_robots
from agentpilot.crawl.types import CrawlOptions
from agentpilot.jobs import dispatch
from agentpilot.jobs.cache import (
    CachePolicy,
    NullScrapeCache,
    ScrapeCacheProtocol,
    cache_key,
    is_cacheable,
)
from agentpilot.jobs.limiter import (
    RETRY_STATUS_CODES,
    HostLimiter,
    InProcessHostLimiter,
    effective_interval_ms,
)
from agentpilot.jobs.options_codec import load_batch_scrape_options, load_crawl_options
from agentpilot.jobs.store import ClaimedTask, JobForWorker, PostgresJobStore
from agentpilot.llm.structured import extract_structured
from crawlpilot.config import DEFAULTS, BrowserConfig
from crawlpilot.extensions.mounts import BlockHooks
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy import NullPrototypes, PrototypeProvider
from crawlpilot.session.ephemeral import run_ephemeral_scrape
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.session.warm_pool import WarmPool
from crawlpilot.spi.driver import BrowserDriver
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.scrape import Document

log = structlog.get_logger(__name__)

_NO_PROTOTYPES = NullPrototypes()


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
        browser_config: BrowserConfig = DEFAULTS,
        prototype_provider: PrototypeProvider = _NO_PROTOTYPES,
        block_hooks: BlockHooks | None = None,
        scrape_cache: ScrapeCacheProtocol | None = None,
        host_limiter: HostLimiter | None = None,
        cache_policy: CachePolicy | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._driver = driver
        self._profiles_root = profiles_root
        self._browser_config = browser_config
        self._prototype_provider = prototype_provider
        self._block_hooks = block_hooks
        self._proxy_pinner = proxy_pinner
        self._warm_pool = warm_pool
        self._lease_ttl_seconds = lease_ttl_seconds
        self._batch_size = batch_size
        self._max_concurrent = max_concurrent
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._cache: ScrapeCacheProtocol = scrape_cache or NullScrapeCache()
        # In-process by default so a deployment without Redis still paces
        # correctly for its own single worker -- see `jobs.limiter`.
        self._limiter: HostLimiter = host_limiter or InProcessHostLimiter()
        self._cache_policy = cache_policy or CachePolicy()
        # Robots parsers, memoized per origin for this worker's lifetime. They
        # were refetched once per frontier expansion, which meant a 500-page
        # crawl of one host fetched the same robots.txt 500 times -- and now
        # that `Crawl-delay` is read on the scrape path too, that would have
        # doubled.
        self._robots: dict[str, RobotFileParser | None] = {}
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
        # Recomputed every tick rather than once at construction: memory
        # pressure is a property of the moment, and the whole point is to admit
        # fewer tasks during the tick where the box is struggling.
        semaphore = asyncio.Semaphore(dispatch.permits(self._max_concurrent))
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
            policy = self._policy_for(crawl_options.cache_mode, crawl_options.max_age_ms)
        else:
            batch_options = load_batch_scrape_options(job.options)
            scrape_options = batch_options.scrape_options
            policy = self._policy_for(batch_options.cache_mode, batch_options.max_age_ms)

        domain = urlparse(task.url).hostname
        if not domain:
            await self._store.fail_task(
                task.task_id, task.lock, f"cannot determine domain from url {task.url!r}"
            )
            return

        key = (
            cache_key(tenant=job.tenant, url=task.url, options=scrape_options)
            if is_cacheable(scrape_options)
            else None
        )
        document: Document | None = None
        if key is not None and policy.may_read:
            document = await self._cache.get(key, max_age_ms=policy.max_age_ms)
            if document is not None:
                log.info("crawl_worker_loop.cache_hit", task_id=task.task_id, url=task.url)

        if document is None:
            # Pace *only* on a miss. Politeness is about requests this service
            # actually makes to a host, and a cache hit makes none -- waiting
            # before serving one would be a delay that protects nobody.
            await self._pace(domain, crawl_options)

            document, _screenshot = await run_ephemeral_scrape(
                browser_config=self._browser_config,
                prototype_provider=self._prototype_provider,
                block_hooks=self._block_hooks,
                structured_extractor=extract_structured,
                scope=job.tenant,
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
            self._record_host_response(domain, document)
            if key is not None and policy.may_write and document.error is None:
                # Never cache a failure. An error document is a statement about
                # one attempt, not about the page, and storing it would turn a
                # transient blip into an hour of confidently-served failures.
                await self._cache.put(
                    key, tenant=job.tenant, url=task.url, document=document
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

    def _policy_for(self, mode: str, max_age_ms: int | None) -> CachePolicy:
        """The job's own cache mode, with the deployment's default filling in an
        unset `max_age_ms`. The *mode* always comes from the job: a caller who
        asked for `bypass` on one crawl means it, whatever the deployment
        prefers."""

        return CachePolicy(
            mode=mode,  # type: ignore[arg-type]
            max_age_ms=max_age_ms
            if max_age_ms is not None
            else self._cache_policy.max_age_ms,
        )

    async def _pace(self, host: str, options: CrawlOptions | None) -> None:
        """Wait out this host's interval before making a request to it.

        `batch_scrape` jobs have no `CrawlOptions` and so no `delay_ms`, but they
        still get the robots.txt `Crawl-delay`: a host that published a rate
        limit meant it regardless of which of our endpoints is doing the asking.
        """

        robots_parser = None
        if options is None or not options.ignore_robots_txt:
            robots_parser = await self._robots_for(f"https://{host}")
        interval_ms = effective_interval_ms(
            delay_ms=options.delay_ms if options is not None else None,
            robots_crawl_delay_seconds=robots_crawl_delay(robots_parser),
        )
        if interval_ms:
            await self._limiter.acquire(host, interval_ms=interval_ms)

    def _record_host_response(self, host: str, document: Document) -> None:
        """Feed the response's status back into this host's backoff.

        Read off `document.metadata.status_code` rather than from an exception:
        a 429 is a *successful* scrape as far as the pipeline is concerned -- it
        returned a page, the page just says to come back later -- so it never
        raises and would otherwise pass through unnoticed.
        """

        status = document.metadata.status_code if document.metadata else None
        if status is None:
            return
        if status in RETRY_STATUS_CODES:
            self._limiter.penalize(host)
        elif status < 400:
            self._limiter.reward(host)

    async def _robots_for(self, origin: str) -> RobotFileParser | None:
        """Memoized per origin. `None` is a legitimate cached value -- it means
        the fetch failed and the policy is fail-open (see `crawl.robots.fetch`)
        -- so membership is tested rather than truthiness, or every page of a
        robots-less host would refetch it."""

        if origin not in self._robots:
            self._robots[origin] = await fetch_robots(origin, EgressPolicy())
        return self._robots[origin]

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
            robots_parser = await self._robots_for(_origin(seed_url))

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
