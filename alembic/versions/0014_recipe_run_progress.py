"""what a long-running recipe run is doing, while it is doing it

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An onboarding build drives a real browser through an agent loop and takes
    # minutes. Everything it learns along the way -- which fields it has found,
    # what it is trying next -- existed only in the worker's memory until the
    # run finished, so the only thing the UI could show for those minutes was a
    # spinner.
    #
    # Its own column rather than partial writes into `data`, which is the
    # run's *result*: a reader polling mid-run would otherwise see a value with
    # a completely different shape depending on when they looked.
    op.execute("ALTER TABLE recipe_runs ADD COLUMN progress JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE recipe_runs DROP COLUMN IF EXISTS progress")
