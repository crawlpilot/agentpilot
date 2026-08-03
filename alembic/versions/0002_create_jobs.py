"""create jobs, crawl_tasks, documents tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # TEXT + CHECK instead of a native Postgres ENUM, matching 0001's house
    # style: adding a status value later is an ALTER TABLE ... DROP/ADD
    # CONSTRAINT, not a fraught ALTER TYPE.
    # TEXT primary keys throughout (app-generated via uuid.uuid4()), matching
    # 0001's api_keys.key_id -- deliberately not the native Postgres UUID
    # type, so nothing here depends on an extension (e.g. pgcrypto's
    # gen_random_uuid()) beyond what 0001 already required.
    op.execute(
        """
        CREATE TABLE jobs (
            job_id          TEXT PRIMARY KEY,
            tenant          TEXT NOT NULL,
            job_type        TEXT NOT NULL CHECK (job_type IN ('crawl', 'batch_scrape')),
            status          TEXT NOT NULL
                            CHECK (status IN
                                ('queued', 'scraping', 'completed', 'failed', 'cancelled')),
            url             TEXT,
            options         JSONB NOT NULL,
            webhook_url     TEXT,
            webhook_headers JSONB,
            webhook_events  TEXT[],
            webhook_secret  TEXT,
            total           INT NOT NULL DEFAULT 0,
            completed       INT NOT NULL DEFAULT 0,
            failed          INT NOT NULL DEFAULT 0,
            discovery_done  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL,
            started_at      TIMESTAMPTZ,
            finished_at     TIMESTAMPTZ,
            error           TEXT
        )
        """
    )
    op.execute("CREATE INDEX idx_jobs_tenant ON jobs (tenant, created_at DESC)")

    op.execute(
        """
        CREATE TABLE crawl_tasks (
            task_id     TEXT PRIMARY KEY,
            job_id      TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
            url         TEXT NOT NULL,
            depth       INT NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'active', 'completed', 'failed')),
            priority    INT NOT NULL DEFAULT 0,
            attempts    INT NOT NULL DEFAULT 0,
            lock        TEXT,
            locked_at   TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            error       TEXT,
            -- The dedup mechanism itself: INSERT ... ON CONFLICT (job_id, url)
            -- DO NOTHING gives exactly-once enqueue per URL for free, no
            -- separate seen-set bookkeeping needed.
            UNIQUE (job_id, url)
        )
        """
    )
    # Partial index on the claim query's own WHERE clause -- keeps
    # `claim_tasks_batch`'s `FOR UPDATE SKIP LOCKED` scan bounded to queued
    # rows as the table grows, instead of scanning completed/failed history.
    op.execute(
        "CREATE INDEX idx_crawl_tasks_claim ON crawl_tasks (status, priority, created_at) "
        "WHERE status = 'queued'"
    )
    op.execute("CREATE INDEX idx_crawl_tasks_job ON crawl_tasks (job_id)")

    op.execute(
        """
        CREATE TABLE documents (
            -- `seq` (not `document_id`, which is an app-generated UUID with
            -- no relationship to insertion order) is the pagination sort
            -- key: keyset pagination needs a key that is monotonic with
            -- insertion, or a row committed after a client's last poll could
            -- sort below their cursor and be silently skipped forever, since
            -- the client never re-queries below a cursor it has already
            -- advanced past. BIGSERIAL has one known, accepted residual
            -- window (a lower seq can commit fractionally after a higher one
            -- under concurrent transactions), noted rather than engineered
            -- away here -- same class of tradeoff as `egress/httpx_guard
            -- .py`'s documented DNS-rebinding TOCTOU gap.
            seq                     BIGSERIAL PRIMARY KEY,
            document_id             TEXT NOT NULL UNIQUE,
            job_id                  TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
            task_id                 TEXT REFERENCES crawl_tasks (task_id) ON DELETE SET NULL,
            url                     TEXT NOT NULL,
            status_code             INT,
            title                   TEXT,
            markdown                TEXT,
            text                    TEXT,
            html                    TEXT,
            raw_html                TEXT,
            links                   JSONB,
            screenshot_artifact_id  TEXT,
            tier_used               TEXT,
            node_id                 TEXT,
            duration_ms             FLOAT,
            error                   TEXT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Keyset pagination on seq, not OFFSET -- documents are inserted
    # concurrently with a caller polling GET /v1/crawl/{id}, so an OFFSET-based
    # page can skip or repeat rows; a `WHERE job_id = %s AND seq > %s` cursor
    # can't (modulo the BIGSERIAL commit-order caveat noted above).
    op.execute("CREATE INDEX idx_documents_job_pagination ON documents (job_id, seq)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_documents_job_pagination")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP INDEX IF EXISTS idx_crawl_tasks_job")
    op.execute("DROP INDEX IF EXISTS idx_crawl_tasks_claim")
    op.execute("DROP TABLE IF EXISTS crawl_tasks")
    op.execute("DROP INDEX IF EXISTS idx_jobs_tenant")
    op.execute("DROP TABLE IF EXISTS jobs")
