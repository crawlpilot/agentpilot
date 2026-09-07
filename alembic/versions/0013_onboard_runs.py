"""the onboard run kind, and a run that is waiting on a human

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `onboard` is the v2 build: schema + URL in, a verified v2 document out.
    # It is a new kind rather than a redefinition of `build` because the two
    # coexist until the v1 stack is retired, and a run's kind is how the worker
    # decides which engine to dispatch to.
    op.execute("ALTER TABLE recipe_runs DROP CONSTRAINT recipe_runs_kind_check")
    op.execute(
        "ALTER TABLE recipe_runs ADD CONSTRAINT recipe_runs_kind_check "
        "CHECK (kind IN ('build', 'replay', 'heal', 'codegen', 'onboard'))"
    )

    # `needs_input` is a run parked mid-build waiting for a human to resolve a
    # field the agent could not locate.
    #
    # It has to be a distinct status rather than a flag on a `running` row.
    # `reclaim_stale_runs` requeues any `running` row whose `locked_at` is older
    # than `stale_after_seconds` (120s by default) on the assumption that its
    # worker died -- so a run waiting on a person would be reclaimed and
    # restarted from the top every two minutes, forever, and the human's answer
    # would arrive for a run that no longer exists.
    #
    # Being its own status excludes it from both queries for free:
    # `claim_runs_batch` selects `status = 'queued'` and `reclaim_stale_runs`
    # selects `status = 'running'`. Neither needs to learn about parking.
    op.execute("ALTER TABLE recipe_runs DROP CONSTRAINT recipe_runs_status_check")
    op.execute(
        "ALTER TABLE recipe_runs ADD CONSTRAINT recipe_runs_status_check "
        "CHECK (status IN "
        "('queued', 'running', 'completed', 'failed', 'cancelled', 'needs_input'))"
    )

    # What the run is waiting to be told: the unresolved fields, why each one
    # failed, and the `step_trace` that distinguishes "the selector was wrong"
    # from "the reveal click never ran". Cleared when the run resumes.
    op.execute("ALTER TABLE recipe_runs ADD COLUMN pending_asks JSONB")
    # When the park expires. A parked run holds a warm identity (one of eight
    # per tenant+domain), a browser and a proxy pin, so it cannot wait forever
    # -- past this the worker resumes it autonomously with those fields
    # unresolved.
    op.execute("ALTER TABLE recipe_runs ADD COLUMN parked_until TIMESTAMPTZ")
    op.execute(
        "CREATE INDEX idx_recipe_runs_parked ON recipe_runs (parked_until) "
        "WHERE status = 'needs_input'"
    )


def downgrade() -> None:
    # Rows in the new states would violate the narrower constraints, so they
    # are resolved before it is reimposed: a parked run becomes a failed one
    # (it was never going to finish without the answer it was waiting for), and
    # an onboard run is dropped -- there is no v1 kind it could honestly become.
    op.execute(
        "UPDATE recipe_runs SET status = 'failed', "
        "error = COALESCE(error, 'run was parked awaiting human input at downgrade'), "
        "finished_at = COALESCE(finished_at, now()) "
        "WHERE status = 'needs_input'"
    )
    op.execute("DELETE FROM recipe_runs WHERE kind = 'onboard'")

    op.execute("DROP INDEX IF EXISTS idx_recipe_runs_parked")
    op.execute("ALTER TABLE recipe_runs DROP COLUMN IF EXISTS parked_until")
    op.execute("ALTER TABLE recipe_runs DROP COLUMN IF EXISTS pending_asks")

    op.execute("ALTER TABLE recipe_runs DROP CONSTRAINT recipe_runs_status_check")
    op.execute(
        "ALTER TABLE recipe_runs ADD CONSTRAINT recipe_runs_status_check "
        "CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))"
    )
    op.execute("ALTER TABLE recipe_runs DROP CONSTRAINT recipe_runs_kind_check")
    op.execute(
        "ALTER TABLE recipe_runs ADD CONSTRAINT recipe_runs_kind_check "
        "CHECK (kind IN ('build', 'replay', 'heal', 'codegen'))"
    )
