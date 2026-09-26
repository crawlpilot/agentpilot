"""add jobs.adaptive_state and jobs.stop_reason

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-26
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Columns on `jobs` rather than a table of their own: the state is strictly
    # 1:1 with a crawl job, lives exactly as long as it does, and the worker reads
    # it in the same transaction it writes it. A separate table would buy a join
    # and a second ON DELETE CASCADE for nothing.
    #
    # `adaptive_state` is bounded by construction -- every collection in
    # `agentpilot.crawl.adaptive.AdaptiveState` has a cap, so this column does not
    # grow with the crawl. That matters because the worker does a locked
    # read-modify-write of it once per completed page: an unbounded blob would
    # turn a 500-page crawl into hundreds of megabytes of transaction I/O.
    #
    # Nullable, so every existing job row stays valid and a crawl that never
    # asked for adaptive stopping never allocates one.
    op.execute("ALTER TABLE jobs ADD COLUMN adaptive_state JSONB")

    # Why a crawl ended, in words, for any crawl that did not simply exhaust its
    # budget. A crawl that stops at 40 of a permitted 500 pages and cannot say why
    # is one nobody will trust the next time, so the reason is a first-class
    # column rather than a log line.
    op.execute("ALTER TABLE jobs ADD COLUMN stop_reason TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS adaptive_state")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS stop_reason")
