"""add documents.extract_warning

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive, nullable: a non-fatal degradation of an extraction that still
    # produced a result (today, page content truncated to fit the model's input
    # budget). `extract_error` cannot carry it -- that field means "there is no
    # result", and this one means "there is a result, computed over part of the
    # page". Nullable so every existing row stays valid.
    op.execute("ALTER TABLE documents ADD COLUMN extract_warning TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS extract_warning")
