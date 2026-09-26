"""add documents.tables

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-26
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive and nullable, like `entities` and `structured_data` before it. JSONB
    # rather than a normalized table-and-rows pair: the payload is read back whole,
    # by the caller that asked for it, and never queried across documents -- so
    # rows and columns here would buy joins and buy nothing.
    op.execute("ALTER TABLE documents ADD COLUMN tables JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS tables")
