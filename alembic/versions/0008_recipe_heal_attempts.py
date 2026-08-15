"""add recipes.heal_attempts

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Consecutive heal cycles that did not restore health, so the worker can
    # stop auto-healing a recipe once it exhausts AGENTPILOT_RECIPE_MAX_HEAL_
    # ATTEMPTS (previously read from env but never enforced -- a heal-thrash /
    # LLM-spend vector, and the reason `broken` had no way to be reached).
    # NOT NULL DEFAULT 0 backfills existing rows to "no failed heals yet".
    op.execute("ALTER TABLE recipes ADD COLUMN heal_attempts INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE recipes DROP COLUMN IF EXISTS heal_attempts")
