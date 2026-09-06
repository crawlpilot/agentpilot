"""extraction jobs: one submission of N urls against a published recipe

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-06
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A recipe was, until now, something that ran *on its own schedule against
    # its own url_pattern*. That is the right model for monitoring one page and
    # the wrong one for a catalogue: a marketplace scraper is picked by someone
    # who already has the URLs, and the recipe is the extraction logic they
    # want applied to them. `recipe/v2/replay.py` was written for exactly that
    # -- it takes the URL as `RunInput`, not from the recipe -- and this is the
    # table that lets a caller supply one.
    #
    # A job is the *submission*; the runs are the work. Splitting them this way
    # means the existing `recipe_runs` queue does the executing, with its claim,
    # lock, retry and SKIP LOCKED semantics already proven by four other run
    # kinds. A second queue would have had to reproduce all of it.
    op.execute(
        """
        CREATE TABLE recipe_jobs (
            job_id       TEXT PRIMARY KEY,
            recipe_id    TEXT REFERENCES recipes (recipe_id) ON DELETE CASCADE,
            tenant       TEXT NOT NULL,
            -- Which version answered. A recipe is healed and re-versioned
            -- underneath a catalogue entry; without this, results from before
            -- and after a heal are indistinguishable and neither can be
            -- trusted to mean what the schema now says.
            recipe_version INT NOT NULL,
            total        INT NOT NULL,
            metadata     JSONB,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_recipe_jobs_tenant ON recipe_jobs (tenant, created_at DESC)")
    op.execute("CREATE INDEX ix_recipe_jobs_recipe ON recipe_jobs (recipe_id, created_at DESC)")

    # Real columns rather than keys inside `params`, for the reason 0011 gives
    # for `domain`/`page_type`: these are what a job view filters and groups on,
    # and a JSONB probe per row is the wrong shape for a listing.
    #
    # `job_id` is also what separates a *submitted* run from a scheduled one.
    # That distinction is load-bearing beyond bookkeeping: a scheduled replay
    # marks the recipe healthy or degraded, and a run against a URL somebody
    # pasted must not -- otherwise one bad submission marks a public template
    # broken for every tenant that can see it.
    op.execute(
        "ALTER TABLE recipe_runs ADD COLUMN job_id TEXT "
        "REFERENCES recipe_jobs (job_id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE recipe_runs ADD COLUMN url TEXT")
    op.execute("CREATE INDEX ix_recipe_runs_job ON recipe_runs (job_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_recipe_runs_job")
    op.execute("ALTER TABLE recipe_runs DROP COLUMN IF EXISTS url")
    op.execute("ALTER TABLE recipe_runs DROP COLUMN IF EXISTS job_id")
    op.execute("DROP INDEX IF EXISTS ix_recipe_jobs_recipe")
    op.execute("DROP INDEX IF EXISTS ix_recipe_jobs_tenant")
    op.execute("DROP TABLE IF EXISTS recipe_jobs")
