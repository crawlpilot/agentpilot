"""Postgres-backed job queue for `/v1/crawl` and `/v1/batch/scrape` --
`SELECT ... FOR UPDATE SKIP LOCKED` claiming, ported from Firecrawl's own
Postgres-backed "NuQ" queue (`apps/api/src/services/worker/nuq.ts`),
simplified for this platform's scale (no RabbitMQ prefetch tier -- a
crawl-worker replica claims directly against Postgres).

Postgres, not Redis: this is durable work-item state that must survive a
crawl-worker restart without silently losing or double-processing a task --
the same "durable tenant-owned data, not ephemeral coordination" reasoning
`agentpilot.auth.store`'s module docstring gives for keeping `api_keys` out of
Redis. Schema: `alembic/versions/0002_create_jobs.py`.

Firecrawl's "group" concept (tracking a crawl's constituent per-URL jobs) is
realized here as `crawl_tasks.job_id` plus the running `jobs.total/completed/
failed` counters this store maintains transactionally -- no separate group
table, since every task in this queue belongs to exactly one job (this queue
has no other job types sharing the table the way NuQ's `group_id` needs to
generalize across).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agentpilot.spi.jobs import Job, JobId, JobStatus, JobType
from agentpilot.spi.scrape import Document, DocumentMetadata
from agentpilot.spi.webhook import WebhookConfig

if TYPE_CHECKING:
    # Deferred at runtime for the same reason `agentpilot.auth.store` defers
    # its psycopg_pool import: `agentpilot.jobs` is on the import path of
    # `monolith`/`gateway`/crawl-worker roles regardless of whether the
    # `postgres` extra happens to be installed in a given image, and a
    # module-level import here would crash boot on ModuleNotFoundError
    # before any role/config check runs.
    from psycopg_pool import AsyncConnectionPool


@dataclass
class ClaimedTask:
    """One row `claim_tasks_batch` moved `queued -> active` and locked. All
    tasks claimed together in one `claim_tasks_batch` call share the same
    `lock` token -- the token only needs to prove *this claim operation*
    still owns the row (guarding against a task `reclaim_stale_tasks` already
    took back), not to individually distinguish tasks within one claim."""

    task_id: str
    job_id: str
    url: str
    depth: int
    priority: int
    attempts: int
    lock: str


def _job_from_row(row: dict[str, Any]) -> Job:
    return Job(
        job_id=JobId(row["job_id"]),
        tenant=row["tenant"],
        job_type=row["job_type"],
        status=row["status"],
        url=row["url"],
        total=row["total"],
        completed=row["completed"],
        failed=row["failed"],
        discovery_done=row["discovery_done"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
    )


def _document_from_row(row: dict[str, Any]) -> Document:
    metadata = None
    if row["status_code"] is not None or row["tier_used"] is not None:
        metadata = DocumentMetadata(
            title=row["title"],
            status_code=row["status_code"],
            tier_used=row["tier_used"],
            node_id=row["node_id"],
            duration_ms=row["duration_ms"],
            source_url=row["url"],
        )
    return Document(
        document_id=row["document_id"],
        url=row["url"],
        markdown=row["markdown"],
        text=row["text"],
        html=row["html"],
        raw_html=row["raw_html"],
        links=tuple(row["links"] or ()),
        screenshot_artifact_id=row["screenshot_artifact_id"],
        metadata=metadata,
        error=row["error"],
    )


_JOB_COLUMNS = (
    "job_id, tenant, job_type, status, url, total, completed, failed, "
    "discovery_done, created_at, started_at, finished_at, error"
)

_DOCUMENT_COLUMNS = (
    "document_id, url, status_code, title, markdown, text, html, raw_html, links, "
    "screenshot_artifact_id, tier_used, node_id, duration_ms, error"
)


class PostgresJobStore:
    """Raw `psycopg` SQL against `jobs`/`crawl_tasks`/`documents`, matching
    `agentpilot.auth.store.PostgresApiKeyStore`'s shape exactly (async
    `connect()` factory, `dict_row`, no ORM)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresJobStore:
        """Async factory, not a plain constructor -- see `PostgresApiKeyStore
        .connect()`'s docstring for why `AsyncConnectionPool.open()` can only
        be awaited from inside a coroutine."""

        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"autocommit": True},
        )
        await pool.open(wait=True, timeout=10.0)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # --- job lifecycle ---

    async def create_job(
        self,
        tenant: str,
        job_type: JobType,
        url: str | None,
        options: dict[str, Any],
        webhook: WebhookConfig | None,
        *,
        discovery_done: bool = False,
    ) -> Job:
        """`discovery_done=True` is for `batch_scrape` callers: that job
        type's entire "frontier" is the explicit URL list passed to
        `enqueue_tasks` right after creation, so there's no separate
        discovery phase to wait on -- `try_finalize_job` would otherwise
        never fire for it. `crawl` callers leave this `False` (the default)
        and call `mark_discovery_done()` once link-following stops finding
        new in-scope URLs."""

        from psycopg.types.json import Jsonb

        job_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO jobs (
                    job_id, tenant, job_type, status, url, options,
                    webhook_url, webhook_headers, webhook_events, webhook_secret,
                    discovery_done, created_at
                )
                VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    tenant,
                    job_type,
                    url,
                    Jsonb(options),
                    webhook.url if webhook else None,
                    Jsonb(webhook.headers) if webhook else None,
                    list(webhook.events) if webhook else None,
                    webhook.secret if webhook else None,
                    discovery_done,
                    created_at,
                ),
            )
        return Job(
            job_id=JobId(job_id),
            tenant=tenant,
            job_type=job_type,
            status="queued",
            url=url,
            total=0,
            completed=0,
            failed=0,
            discovery_done=discovery_done,
            created_at=created_at,
            started_at=None,
            finished_at=None,
            error=None,
        )

    async def get_job(self, job_id: str, tenant: str) -> Job | None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = %s AND tenant = %s",
                    (job_id, tenant),
                )
                row = await cur.fetchone()
        return _job_from_row(row) if row else None

    async def cancel_job(self, job_id: str, tenant: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = %s "
                    "WHERE job_id = %s AND tenant = %s AND status IN ('queued', 'scraping')",
                    (datetime.now(UTC), job_id, tenant),
                )
                return cur.rowcount > 0

    async def mark_started(self, job_id: str) -> None:
        """First transition out of `queued`, called once `enqueue_tasks`
        seeds the job's first batch. Needed because `try_finalize_job`'s
        guard only fires from `status = 'scraping'` -- a job whose seeding
        produced zero tasks (e.g. `batch_scrape` with an empty URL list)
        would otherwise never leave `queued` and never finalize at all."""

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'scraping', started_at = %s "
                "WHERE job_id = %s AND status = 'queued'",
                (datetime.now(UTC), job_id),
            )

    async def mark_discovery_done(self, job_id: str) -> None:
        """Crawl-only: called once the frontier is fully known (no more
        `enqueue_tasks` calls will happen for this job). `batch_scrape` jobs
        are created with `discovery_done` already true (see `create_job`'s
        caller in `gateway/routes/batch_scrape.py`) and never call this."""

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE jobs SET discovery_done = TRUE WHERE job_id = %s", (job_id,)
            )

    async def try_finalize_job(self, job_id: str) -> JobStatus | None:
        """Idempotent terminal-status flip via a `RETURNING`-guarded
        conditional `UPDATE`, the same pattern `PostgresApiKeyStore.revoke()`
        uses for `revoked_at IS NULL` -- only the caller whose `UPDATE`
        actually matched a row gets a non-`None` result back, which is what
        makes it safe to call this after every single task completion from
        multiple concurrent crawl-worker replicas without double-firing the
        terminal webhook.

        `failed` only if every task failed (`completed = 0`) -- a crawl or
        batch that partially succeeds finalizes as `completed`, matching
        Firecrawl's own behavior of returning whatever succeeded rather than
        failing the whole job over some 404s."""

        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE jobs SET
                        status = CASE WHEN failed > 0 AND completed = 0 THEN 'failed'
                                      ELSE 'completed' END,
                        finished_at = now()
                    WHERE job_id = %s AND status = 'scraping' AND discovery_done
                        AND completed + failed >= total
                    RETURNING status
                    """,
                    (job_id,),
                )
                row = await cur.fetchone()
        return row["status"] if row else None

    # --- task queue ---

    async def enqueue_tasks(
        self, job_id: str, urls: list[str], depth: int = 0, priority: int = 0
    ) -> int:
        """Bulk enqueue via a single `INSERT ... SELECT ... FROM unnest(...)
        ON CONFLICT DO NOTHING RETURNING task_id` -- one round trip
        regardless of batch size, and `len()` of the returned rows is exactly
        the count of *new* tasks (conflicting URLs produce no `RETURNING`
        row), which is what `jobs.total` should be bumped by. Safe to call
        repeatedly with overlapping URL sets: the `UNIQUE (job_id, url)`
        constraint is the entire dedup mechanism, and crawl's frontier
        expansion does call this repeatedly with URLs it has already seen."""

        if not urls:
            return 0
        task_ids = [str(uuid.uuid4()) for _ in urls]
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO crawl_tasks (task_id, job_id, url, depth, priority)
                    SELECT t, %s, u, %s, %s FROM unnest(%s::text[], %s::text[]) AS x(t, u)
                    ON CONFLICT (job_id, url) DO NOTHING
                    RETURNING task_id
                    """,
                    (job_id, depth, priority, task_ids, urls),
                )
                inserted = await cur.fetchall()
            if inserted:
                await conn.execute(
                    "UPDATE jobs SET total = total + %s WHERE job_id = %s",
                    (len(inserted), job_id),
                )
        return len(inserted)

    async def claim_tasks_batch(self, limit: int) -> list[ClaimedTask]:
        from psycopg.rows import dict_row

        lock = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH next AS (
                        SELECT task_id FROM crawl_tasks
                        WHERE status = 'queued'
                        ORDER BY priority ASC, created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE crawl_tasks t
                    SET status = 'active', lock = %s, locked_at = now(), attempts = attempts + 1
                    FROM next
                    WHERE t.task_id = next.task_id
                    RETURNING t.task_id, t.job_id, t.url, t.depth, t.priority, t.attempts
                    """,
                    (limit, lock),
                )
                rows = await cur.fetchall()
        return [
            ClaimedTask(
                task_id=r["task_id"],
                job_id=r["job_id"],
                url=r["url"],
                depth=r["depth"],
                priority=r["priority"],
                attempts=r["attempts"],
                lock=lock,
            )
            for r in rows
        ]

    async def renew_lock(self, task_id: str, lock: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE crawl_tasks SET locked_at = now() "
                    "WHERE task_id = %s AND lock = %s AND status = 'active'",
                    (task_id, lock),
                )
                return cur.rowcount > 0

    async def reclaim_stale_tasks(self, stale_after_seconds: float) -> int:
        """Crash recovery: any `active` task whose lock hasn't been renewed
        recently resets to `queued` for a different claimer -- the queue-level
        analogue of `agentpilot.session.reaper.Reaper`'s expired-lease
        reclamation. Called at the top of every `CrawlWorkerLoop` tick."""

        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE crawl_tasks SET status = 'queued', lock = NULL, locked_at = NULL "
                    "WHERE status = 'active' AND locked_at < now() - %s * INTERVAL '1 second'",
                    (stale_after_seconds,),
                )
                return cur.rowcount

    async def complete_task(self, task_id: str, lock: str, document: Document) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT job_id FROM crawl_tasks WHERE task_id = %s AND lock = %s "
                    "AND status = 'active'",
                    (task_id, lock),
                )
                row = await cur.fetchone()
                if row is None:
                    # Reclaimed out from under this attempt (stale-lock
                    # timeout) -- discard the result rather than error: the
                    # task is already back in `queued` (or claimed by a
                    # different attempt) for someone else to retry.
                    return
                job_id = row[0]

                meta = document.metadata
                await cur.execute(
                    f"INSERT INTO documents (job_id, task_id, {_DOCUMENT_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        job_id,
                        task_id,
                        document.document_id,
                        document.url,
                        meta.status_code if meta else None,
                        meta.title if meta else None,
                        document.markdown,
                        document.text,
                        document.html,
                        document.raw_html,
                        Jsonb(list(document.links)),
                        document.screenshot_artifact_id,
                        meta.tier_used if meta else None,
                        meta.node_id if meta else None,
                        meta.duration_ms if meta else None,
                        document.error,
                    ),
                )
                await cur.execute(
                    "UPDATE crawl_tasks SET status = 'completed', finished_at = now() "
                    "WHERE task_id = %s AND lock = %s",
                    (task_id, lock),
                )
                await cur.execute(
                    "UPDATE jobs SET completed = completed + 1 WHERE job_id = %s", (job_id,)
                )

    async def fail_task(
        self, task_id: str, lock: str, error: str, max_attempts: int = 3
    ) -> None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT job_id, attempts FROM crawl_tasks "
                    "WHERE task_id = %s AND lock = %s AND status = 'active'",
                    (task_id, lock),
                )
                row = await cur.fetchone()
                if row is None:
                    return  # same "already reclaimed" case as complete_task
                if row["attempts"] < max_attempts:
                    await cur.execute(
                        "UPDATE crawl_tasks SET status = 'queued', lock = NULL, "
                        "locked_at = NULL, error = %s WHERE task_id = %s AND lock = %s",
                        (error, task_id, lock),
                    )
                    return
                await cur.execute(
                    "UPDATE crawl_tasks SET status = 'failed', finished_at = now(), error = %s "
                    "WHERE task_id = %s AND lock = %s",
                    (error, task_id, lock),
                )
                await cur.execute(
                    "UPDATE jobs SET failed = failed + 1 WHERE job_id = %s", (row["job_id"],)
                )

    # --- results ---

    async def list_documents(
        self, job_id: str, tenant: str, after: str | None, limit: int = 100
    ) -> tuple[list[Document], str | None]:
        """Keyset pagination on `documents.seq` (an app-invisible monotonic
        counter), not `document_id` or `OFFSET` -- see `0002_create_jobs.py`'s
        comment on why `seq` exists. `tenant` is enforced via a join back to
        `jobs` rather than a duplicated column on `documents`, so job
        ownership has exactly one source of truth."""

        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                params: tuple[Any, ...] = (job_id, tenant)
                cursor_clause = ""
                if after is not None:
                    cursor_clause = "AND d.seq > %s"
                    params += (int(after),)
                await cur.execute(
                    f"""
                    SELECT d.seq, {", ".join(f"d.{c}" for c in _DOCUMENT_COLUMNS.split(", "))}
                    FROM documents d JOIN jobs j ON j.job_id = d.job_id
                    WHERE d.job_id = %s AND j.tenant = %s {cursor_clause}
                    ORDER BY d.seq LIMIT %s
                    """,
                    (*params, limit),
                )
                rows = await cur.fetchall()
        docs = [_document_from_row(r) for r in rows]
        next_cursor = str(rows[-1]["seq"]) if len(rows) == limit else None
        return docs, next_cursor
