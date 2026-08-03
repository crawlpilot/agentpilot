"""add documents.structured_data

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN structured_data JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS structured_data")
