"""create scrape_cache

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-25
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A separate table rather than a reuse of `documents`: that table's rows are
    # job-scoped (`job_id`/`task_id` both NOT NULL with FKs onto `jobs`), so a
    # `/v1/scrape` result has nowhere to live in it, and a cache entry outliving
    # the job it came from would fight the ON DELETE CASCADE that keeps it tidy.
    #
    # `cache_key` is the whole key and is already tenant-scoped -- it is a digest
    # over tenant, URL and every option that changes the result (see
    # `agentpilot.jobs.cache.cache_key`). `tenant` and `url` are stored
    # alongside it as plain columns purely so an operator can see what is in
    # here; nothing reads them.
    #
    # No expiry column, deliberately: freshness is evaluated at read time
    # against the *caller's* `max_age_ms`, because a nightly archival crawl and a
    # price checker legitimately disagree about how old is too old, and one row
    # should serve both.
    op.execute(
        """
        CREATE TABLE scrape_cache (
            cache_key   TEXT PRIMARY KEY,
            tenant      TEXT NOT NULL,
            url         TEXT NOT NULL,
            document    JSONB NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # For the eviction sweep an operator will eventually want ("delete anything
    # older than a day"), and for the per-tenant purge a support request needs.
    op.execute("CREATE INDEX scrape_cache_created_at_idx ON scrape_cache (created_at)")
    op.execute("CREATE INDEX scrape_cache_tenant_idx ON scrape_cache (tenant)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scrape_cache")
