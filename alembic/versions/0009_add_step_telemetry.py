"""add agent_steps telemetry: duration, token usage, screenshot

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive, all nullable -- same discipline as 0007 (thinking): existing
    # rows and any step written without telemetry stay valid, no behavior
    # change. `duration_ms` is the LLM call latency the loop already measures
    # but dropped; `input_tokens`/`output_tokens` come from the completion's
    # `usage` block (also previously discarded). `screenshot` holds the raw
    # viewport PNG captured under vision -- BYTEA, not base64 TEXT, to keep it
    # out of the JSON step-list payload (served by its own image route).
    op.execute("ALTER TABLE agent_steps ADD COLUMN duration_ms INT")
    op.execute("ALTER TABLE agent_steps ADD COLUMN input_tokens INT")
    op.execute("ALTER TABLE agent_steps ADD COLUMN output_tokens INT")
    op.execute("ALTER TABLE agent_steps ADD COLUMN screenshot BYTEA")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_steps DROP COLUMN IF EXISTS screenshot")
    op.execute("ALTER TABLE agent_steps DROP COLUMN IF EXISTS output_tokens")
    op.execute("ALTER TABLE agent_steps DROP COLUMN IF EXISTS input_tokens")
    op.execute("ALTER TABLE agent_steps DROP COLUMN IF EXISTS duration_ms")
