"""add agent_steps.thinking

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive, nullable: the model's raw reasoning trace (`AgentOutput.thinking`)
    # was parsed but dropped before this. Nullable so existing rows -- and any
    # step written without a thinking trace -- stay valid. No behavior change;
    # this is Wave 0 audit/replay instrumentation only.
    op.execute("ALTER TABLE agent_steps ADD COLUMN thinking TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_steps DROP COLUMN IF EXISTS thinking")
