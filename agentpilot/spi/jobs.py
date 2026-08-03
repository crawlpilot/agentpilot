"""Async job types for `/v1/crawl` and `/v1/batch/scrape` -- backed by
`agentpilot.jobs.store.PostgresJobStore`, a new Postgres-backed queue (not
Redis/BullMQ/RabbitMQ: this platform's Redis is reserved for the ephemeral
placement/routing coordination `agentpilot.session`/`agentpilot.placement`
already use it for, and a durable job queue wants Postgres's durability
guarantees regardless -- see `agentpilot/jobs/store.py`'s docstring for the
full reasoning, ported from Firecrawl's own Postgres-backed "NuQ" queue).

A `Job` tracks one `/v1/crawl` or `/v1/batch/scrape` call; its constituent
per-URL work items live in `agentpilot.jobs.store`'s `crawl_tasks` table,
correlated by `job_id` -- there is no separate `spi` type for a task, since
callers only ever see the aggregate `Job` (via `GET /v1/crawl/{id}`) and the
resulting `Document`s (`spi.scrape.Document`), never a task directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NewType

JobType = Literal["crawl", "batch_scrape"]
JobStatus = Literal["queued", "scraping", "completed", "failed", "cancelled"]
JobId = NewType("JobId", str)


@dataclass
class Job:
    job_id: JobId
    tenant: str
    job_type: JobType
    status: JobStatus
    url: str | None
    """The seed URL for a `crawl` job; always `None` for `batch_scrape`
    (which has no single seed, just an explicit URL list)."""
    total: int
    completed: int
    failed: int
    discovery_done: bool
    """`True` once the URL frontier is fully known: immediately at creation
    for `batch_scrape` (the URL list is the whole frontier), only once link
    discovery/crawling stops finding new in-scope URLs for `crawl`. Gates
    `PostgresJobStore.try_finalize_job()` -- a crawl must not be reported
    `completed` just because every *currently queued* task finished if more
    tasks could still be enqueued."""
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
