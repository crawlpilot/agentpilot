"""add documents.fit_markdown and documents.entities

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-25
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both additive and nullable, so every existing row stays valid and a
    # caller who never asks for the new formats never pays for them.
    #
    # `fit_markdown` is a column rather than a replacement for `markdown`
    # deliberately: the two are requested independently (`formats:
    # ["markdown", "fit_markdown"]` returns both), and a caller tuning a
    # relevance query needs to see what the filter discarded. Folding the
    # filtered text into `markdown` would make that impossible and would
    # silently change what every existing crawl row means.
    op.execute("ALTER TABLE documents ADD COLUMN fit_markdown TEXT")
    # JSONB, matching `structured_data`/`extract`: the payload is a mapping of
    # entity kind to a list of matches, and callers filter it by key.
    op.execute("ALTER TABLE documents ADD COLUMN entities JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS fit_markdown")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS entities")
