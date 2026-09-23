"""what a build actually tried, and why each attempt was rejected

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A build that produces a wrong selector, or none, left nothing behind to
    # look at. `progress` (0014) narrates what the run is *doing*; `data` holds
    # what it *got*. Neither says what was proposed and rejected on the way, and
    # the rejection strings are the useful part -- `verify_locators` and
    # `_problems_with` already write them for a person to read ("column 'value'
    # is empty in 9 of 10 rows, so it is not being resolved inside each row"),
    # and they went to a worker's stdout.
    #
    # A separate table rather than more JSONB on `recipe_runs`: a trace is
    # append-only, one row per field per stage, and the prompt and snapshot
    # bodies are large enough that a reader polling the run for its status
    # should not be made to carry them.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_run_artifacts (
            seq          BIGSERIAL PRIMARY KEY,
            run_id       TEXT NOT NULL,
            tenant       TEXT NOT NULL,
            kind         TEXT NOT NULL,
            field        TEXT,
            body         JSONB NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT recipe_run_artifacts_kind_check CHECK (
                kind IN ('trace', 'outline', 'prompt', 'response', 'snapshot')
            )
        )
        """
    )
    # The only read pattern: everything one run left behind, oldest first, which
    # is the order it happened in.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS recipe_run_artifacts_run_idx
            ON recipe_run_artifacts (run_id, seq)
        """
    )
    # Artifacts are diagnostic, not the record of what a recipe collects, so a
    # deleted run takes them with it rather than being kept for their own sake.
    op.execute(
        """
        ALTER TABLE recipe_run_artifacts
            ADD CONSTRAINT recipe_run_artifacts_run_fk
            FOREIGN KEY (run_id) REFERENCES recipe_runs (run_id) ON DELETE CASCADE
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS recipe_run_artifacts")
