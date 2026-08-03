"""`agentpilot.jobs.store.PostgresJobStore` -- against a real local Postgres test
database, same skip idiom as `tests/test_postgres_api_key_store.py` (point
`AGENTPILOT_TEST_DATABASE_URL` at a scratch database with `alembic upgrade
head` already applied; this suite is skipped entirely otherwise).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest

from agentpilot.jobs.store import PostgresJobStore
from agentpilot.spi.scrape import Document, DocumentMetadata
from agentpilot.spi.webhook import WebhookConfig

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
        await conn.execute("DELETE FROM jobs WHERE tenant LIKE 'jobtest-%'")
    await s.close()


def _tenant() -> str:
    return f"jobtest-{uuid.uuid4().hex[:8]}"


def _document(url: str, **overrides) -> Document:
    defaults = dict(
        document_id=str(uuid.uuid4()),
        url=url,
        markdown=f"# {url}",
        metadata=DocumentMetadata(
            title="t", status_code=200, tier_used="basic", node_id="n1",
            duration_ms=12.5, source_url=url,
        ),
    )
    defaults.update(overrides)
    return Document(**defaults)


async def test_create_job_starts_queued_with_zero_totals(store: PostgresJobStore) -> None:
    job = await store.create_job(_tenant(), "crawl", "https://example.com", {"limit": 10}, None)
    assert job.status == "queued"
    assert job.total == 0
    assert job.completed == 0
    assert job.discovery_done is False


async def test_create_job_discovery_done_true_is_for_batch_scrape(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None, discovery_done=True)
    assert job.discovery_done is True
    fetched = await store.get_job(job.job_id, job.tenant)
    assert fetched is not None
    assert fetched.discovery_done is True


async def test_get_job_for_worker_has_no_tenant_filter(store: PostgresJobStore) -> None:
    tenant = _tenant()
    job = await store.create_job(
        tenant, "crawl", "https://example.com", {"url": "https://example.com", "limit": 5}, None
    )
    fetched = await store.get_job_for_worker(job.job_id)
    assert fetched is not None
    assert fetched.tenant == tenant
    assert fetched.options == {"url": "https://example.com", "limit": 5}


async def test_get_job_for_worker_includes_webhook_secret(store: PostgresJobStore) -> None:
    from agentpilot.spi.webhook import WebhookConfig

    webhook = WebhookConfig(url="https://example.com/hook", secret="shh", events=("completed",))
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, webhook)
    fetched = await store.get_job_for_worker(job.job_id)
    assert fetched is not None
    assert fetched.webhook_url == "https://example.com/hook"
    assert fetched.webhook_secret == "shh"
    assert fetched.webhook_events == ("completed",)


async def test_get_job_for_worker_unknown_id_returns_none(store: PostgresJobStore) -> None:
    assert await store.get_job_for_worker("no-such-job") is None


async def test_create_job_persists_webhook_secret_but_get_job_omits_it(
    store: PostgresJobStore,
) -> None:
    tenant = _tenant()
    webhook = WebhookConfig(url="https://example.com/hook", secret="shh")
    job = await store.create_job(tenant, "batch_scrape", None, {}, webhook)
    fetched = await store.get_job(job.job_id, tenant)
    assert fetched is not None
    assert fetched.job_id == job.job_id
    # Job (spi.jobs) deliberately has no webhook field -- callers never see
    # the secret again after job creation, same "shown once" discipline as
    # an API key's plaintext.
    assert not hasattr(fetched, "webhook")


async def test_get_job_is_scoped_to_tenant(store: PostgresJobStore) -> None:
    job = await store.create_job(_tenant(), "crawl", "https://example.com", {}, None)
    assert await store.get_job(job.job_id, _tenant()) is None


async def test_enqueue_tasks_dedups_within_and_across_calls(store: PostgresJobStore) -> None:
    job = await store.create_job(_tenant(), "crawl", "https://example.com", {}, None)
    first = await store.enqueue_tasks(
        job.job_id, ["https://a.example", "https://b.example", "https://a.example"]
    )
    assert first == 2  # the in-batch duplicate doesn't double-count
    second = await store.enqueue_tasks(job.job_id, ["https://a.example", "https://c.example"])
    assert second == 1  # only the new URL counts

    fetched = await store.get_job(job.job_id, job.tenant)
    assert fetched is not None
    assert fetched.total == 3


async def test_claim_tasks_batch_moves_status_and_returns_a_shared_lock(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    await store.enqueue_tasks(job.job_id, ["https://a.example", "https://b.example"])

    claimed = await store.claim_tasks_batch(limit=10)
    assert {t.url for t in claimed} == {"https://a.example", "https://b.example"}
    assert len({t.lock for t in claimed}) == 1  # one claim call, one shared lock token

    # A second claim call must not see the already-active rows.
    again = await store.claim_tasks_batch(limit=10)
    assert again == []


async def test_claim_tasks_batch_is_exclusive_under_concurrent_claimers(
    store: PostgresJobStore,
) -> None:
    """The `FOR UPDATE SKIP LOCKED` claim query's whole reason to exist: two
    concurrent claimers racing the same queued rows must partition them, not
    double-claim any row."""

    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    urls = [f"https://x{i}.example" for i in range(20)]
    await store.enqueue_tasks(job.job_id, urls)

    results = await asyncio.gather(*(store.claim_tasks_batch(limit=15) for _ in range(2)))
    claimed_urls = [t.url for batch in results for t in batch]
    assert len(claimed_urls) == len(set(claimed_urls)) == 20


async def test_complete_task_persists_document_and_bumps_completed_counter(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])
    [task] = await store.claim_tasks_batch(limit=1)

    await store.complete_task(task.task_id, task.lock, _document(task.url))

    fetched = await store.get_job(job.job_id, job.tenant)
    assert fetched is not None
    assert fetched.completed == 1

    docs, next_cursor = await store.list_documents(job.job_id, job.tenant, after=None, limit=10)
    assert [d.url for d in docs] == ["https://a.example"]
    assert docs[0].markdown == "# https://a.example"
    assert next_cursor is None  # fewer rows than the page limit


async def test_complete_task_is_a_no_op_once_lock_no_longer_matches(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])
    [task] = await store.claim_tasks_batch(limit=1)

    await store.reclaim_stale_tasks(stale_after_seconds=0)  # forces the lock to go stale now
    await store.complete_task(task.task_id, task.lock, _document(task.url))

    fetched = await store.get_job(job.job_id, job.tenant)
    assert fetched is not None
    assert fetched.completed == 0  # the stale attempt's result must not count


async def test_fail_task_retries_until_max_attempts_then_terminal_fails(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])

    for _ in range(3):
        [task] = await store.claim_tasks_batch(limit=1)
        await store.fail_task(task.task_id, task.lock, "boom", max_attempts=3)

    fetched = await store.get_job(job.job_id, job.tenant)
    assert fetched is not None
    assert fetched.failed == 1
    assert await store.claim_tasks_batch(limit=10) == []  # terminal, not requeued again


async def test_reclaim_stale_tasks_requeues_active_rows_past_the_threshold(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])
    await store.claim_tasks_batch(limit=1)

    reclaimed = await store.reclaim_stale_tasks(stale_after_seconds=0)
    assert reclaimed == 1

    requeued = await store.claim_tasks_batch(limit=1)
    assert [t.url for t in requeued] == ["https://a.example"]


async def test_try_finalize_job_requires_discovery_done(store: PostgresJobStore) -> None:
    job = await store.create_job(_tenant(), "crawl", "https://example.com", {}, None)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])
    await store.mark_started(job.job_id)
    [task] = await store.claim_tasks_batch(limit=1)
    await store.complete_task(task.task_id, task.lock, _document(task.url))

    # All enqueued tasks are done, but the crawl's frontier isn't known to be
    # exhausted yet -- must not finalize.
    assert await store.try_finalize_job(job.job_id) is None

    await store.mark_discovery_done(job.job_id)
    assert await store.try_finalize_job(job.job_id) == "completed"


async def test_try_finalize_job_is_idempotent_under_concurrent_callers(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None, discovery_done=True)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])
    await store.mark_started(job.job_id)
    [task] = await store.claim_tasks_batch(limit=1)
    await store.complete_task(task.task_id, task.lock, _document(task.url))

    results = await asyncio.gather(*(store.try_finalize_job(job.job_id) for _ in range(5)))
    assert results.count("completed") == 1  # only one caller's UPDATE actually matched
    assert results.count(None) == 4


async def test_try_finalize_job_reports_failed_only_when_nothing_succeeded(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None, discovery_done=True)
    await store.enqueue_tasks(job.job_id, ["https://a.example"])
    await store.mark_started(job.job_id)
    for _ in range(3):
        [task] = await store.claim_tasks_batch(limit=1)
        await store.fail_task(task.task_id, task.lock, "boom", max_attempts=3)

    assert await store.try_finalize_job(job.job_id) == "failed"


async def test_cancel_job_prevents_further_finalization(store: PostgresJobStore) -> None:
    job = await store.create_job(_tenant(), "crawl", "https://example.com", {}, None)
    assert await store.cancel_job(job.job_id, job.tenant) is True
    fetched = await store.get_job(job.job_id, job.tenant)
    assert fetched is not None
    assert fetched.status == "cancelled"
    assert await store.cancel_job(job.job_id, job.tenant) is False  # already terminal


async def test_list_documents_pagination_cursor_advances_by_seq(
    store: PostgresJobStore,
) -> None:
    job = await store.create_job(_tenant(), "batch_scrape", None, {}, None)
    urls = [f"https://p{i}.example" for i in range(5)]
    await store.enqueue_tasks(job.job_id, urls)
    for _ in range(5):
        [task] = await store.claim_tasks_batch(limit=1)
        await store.complete_task(task.task_id, task.lock, _document(task.url))

    first_page, cursor = await store.list_documents(job.job_id, job.tenant, after=None, limit=2)
    assert len(first_page) == 2
    assert cursor is not None

    second_page, cursor2 = await store.list_documents(
        job.job_id, job.tenant, after=cursor, limit=2
    )
    assert len(second_page) == 2
    assert {d.url for d in first_page} & {d.url for d in second_page} == set()

    third_page, cursor3 = await store.list_documents(
        job.job_id, job.tenant, after=cursor2, limit=2
    )
    assert len(third_page) == 1
    assert cursor3 is None  # last page, short of the limit
