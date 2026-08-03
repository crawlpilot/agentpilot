"""`agentpilot.jobs.worker_loop.CrawlWorkerLoop` -- real Patchright driver,
real in-memory `Registry`, real Postgres (`PostgresJobStore`). Skipped
automatically when `AGENTPILOT_TEST_DATABASE_URL` isn't set/reachable, same
idiom as `tests/test_jobs_store.py`.

Small fixture site: `/` links to `/a` and `/b`; `/a` links back to `/`
(already seen) and forward to `/c`; `/b` and `/c` are leaves. Crawling from
`/` with no depth/backward restriction should discover all four pages.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from pytest_httpserver import HTTPServer

from agentpilot.crawl.seed import discover_for_crawl
from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.jobs.options_codec import dump_crawl_options
from agentpilot.jobs.store import PostgresJobStore
from agentpilot.jobs.worker_loop import CrawlWorkerLoop
from agentpilot.session.registry import Registry
from agentpilot.spi.crawl import CrawlOptions
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.scrape import ScrapeOptions

_DATABASE_URL = os.environ.get("AGENTPILOT_TEST_DATABASE_URL")


def _database_reachable() -> bool:
    if not _DATABASE_URL:
        return False
    try:
        with psycopg.connect(_DATABASE_URL, connect_timeout=1):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason="set AGENTPILOT_TEST_DATABASE_URL to a real local Postgres db with "
    "`alembic upgrade head` already applied",
)


@pytest.fixture
async def store():
    assert _DATABASE_URL is not None
    s = await PostgresJobStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM jobs WHERE tenant LIKE 'crawlworker-%'")
    await s.close()


def _tenant() -> str:
    return f"crawlworker-{uuid.uuid4().hex[:8]}"


def _register_fixture_site(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        '<a href="/a">A</a><a href="/b">B</a>', content_type="text/html"
    )
    httpserver.expect_request("/a").respond_with_data(
        '<a href="/">Home</a><a href="/c">C</a>', content_type="text/html"
    )
    httpserver.expect_request("/b").respond_with_data("<p>B leaf</p>", content_type="text/html")
    httpserver.expect_request("/c").respond_with_data("<p>C leaf</p>", content_type="text/html")


async def _seed_crawl_job(store: PostgresJobStore, tenant: str, options: CrawlOptions) -> str:
    """Mirrors what `routes/crawl.py` does at job creation. `CrawlSeedResult
    .urls[0]` is always the seed URL itself (depth 0, per that dataclass's
    own docstring); everything else discovered alongside it (sitemap +
    seed-page links) is one level deeper (depth 1) -- enqueueing the *whole*
    batch at depth=0 would let depth-1 pages' own frontier expansion run
    unrestricted one hop too early under `max_discovery_depth`."""

    job = await store.create_job(
        tenant, "crawl", options.url, dump_crawl_options(options), None
    )
    result = await discover_for_crawl(options, EgressPolicy())
    await store.enqueue_tasks(job.job_id, result.urls[:1], depth=0)
    if len(result.urls) > 1:
        await store.enqueue_tasks(job.job_id, result.urls[1:], depth=1)
    await store.mark_started(job.job_id)
    # See worker_loop.py's module docstring: discovery_done is set once the
    # *initial* seed is enqueued, not once the frontier is fully exhausted --
    # the completed+failed>=total invariant already blocks premature
    # finalization while any active task's own expansion is still pending.
    await store.mark_discovery_done(job.job_id)
    return job.job_id


async def _run_until_terminal(
    loop: CrawlWorkerLoop, store: PostgresJobStore, tenant: str, job_id: str, max_ticks: int = 20
) -> None:
    for _ in range(max_ticks):
        job = await store.get_job(job_id, tenant)
        assert job is not None
        if job.status in ("completed", "failed"):
            return
        await loop.tick()
    raise AssertionError(f"job {job_id} did not reach a terminal status within {max_ticks} ticks")


async def test_crawl_worker_loop_discovers_and_scrapes_the_whole_fixture_site(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer, store: PostgresJobStore
) -> None:
    _register_fixture_site(httpserver)
    tenant = _tenant()
    options = CrawlOptions(
        url=httpserver.url_for("/"),
        limit=10,
        scrape_options=ScrapeOptions(formats=("markdown", "html")),
    )
    job_id = await _seed_crawl_job(store, tenant, options)

    loop = CrawlWorkerLoop(
        store, Registry(), driver, tmp_path, None, poll_interval_seconds=0.01
    )
    await _run_until_terminal(loop, store, tenant, job_id)

    job = await store.get_job(job_id, tenant)
    assert job is not None
    assert job.status == "completed"

    docs, _cursor = await store.list_documents(job_id, tenant, after=None, limit=100)
    urls = {d.url for d in docs}
    assert urls == {
        httpserver.url_for("/"),
        httpserver.url_for("/a"),
        httpserver.url_for("/b"),
        httpserver.url_for("/c"),
    }
    for doc in docs:
        assert doc.markdown is not None


async def test_crawl_worker_loop_respects_max_discovery_depth(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer, store: PostgresJobStore
) -> None:
    _register_fixture_site(httpserver)
    tenant = _tenant()
    options = CrawlOptions(
        url=httpserver.url_for("/"),
        limit=10,
        max_discovery_depth=1,
        scrape_options=ScrapeOptions(formats=("markdown", "html")),
    )
    job_id = await _seed_crawl_job(store, tenant, options)

    loop = CrawlWorkerLoop(
        store, Registry(), driver, tmp_path, None, poll_interval_seconds=0.01
    )
    await _run_until_terminal(loop, store, tenant, job_id)

    docs, _cursor = await store.list_documents(job_id, tenant, after=None, limit=100)
    urls = {d.url for d in docs}
    # depth 0 is the seed; depth-1 tasks (a, b) are enqueued from it and do
    # get scraped, but max_discovery_depth=1 stops *their* own expansion
    # (task.depth=1 >= max_discovery_depth=1), so /c (which only /a links to)
    # is never discovered.
    assert urls == {httpserver.url_for("/"), httpserver.url_for("/a"), httpserver.url_for("/b")}
    assert httpserver.url_for("/c") not in urls
