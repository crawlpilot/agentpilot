"""store the full v2 recipe document

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-06
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The `recipes` row is a v1 shape: one `url_pattern`, a `field_schema`, and
    # `global_setup`/`field_groups` as loose JSON. A v2 document also carries
    # `variants`, `defaults`, `sample_urls`, `status` and a *list* of target
    # matchers, none of which have a column -- so a hand-authored recipe could
    # not round-trip, which is why there was no update endpoint to begin with.
    #
    # Rather than add six columns that only v2 uses, the document is stored
    # whole. The v1 columns stay populated and authoritative for everything
    # that already reads them (the worker, the scheduler, `RecipeOut`), so this
    # is additive: an existing row simply has `document IS NULL` and behaves
    # exactly as before.
    op.execute("ALTER TABLE recipes ADD COLUMN document JSONB")
    # Same on the audit trail, so a rollback restores the whole document and
    # not just the two columns a heal happens to touch.
    op.execute("ALTER TABLE recipe_versions ADD COLUMN document JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE recipes DROP COLUMN IF EXISTS document")
    op.execute("ALTER TABLE recipe_versions DROP COLUMN IF EXISTS document")
