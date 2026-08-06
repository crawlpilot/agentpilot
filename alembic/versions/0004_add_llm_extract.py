"""add documents.extract, documents.extract_error

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN extract JSONB")
    op.execute("ALTER TABLE documents ADD COLUMN extract_error TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS extract_error")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS extract")
