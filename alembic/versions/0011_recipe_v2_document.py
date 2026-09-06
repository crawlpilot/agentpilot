"""store the full v2 recipe document, and marketplace metadata

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

    # Marketplace metadata: what a recipe is *for*, so a catalogue can be
    # browsed by site and page kind ("zara.com / PDP") rather than by opaque
    # recipe name.
    #
    # Real columns, not keys inside `document`, because the whole point is to
    # filter and group on them -- a JSONB probe on every row is the wrong shape
    # for a listing, and an index on it is worse than an index on a text column.
    #
    # `domain` is derived from the first sample URL at save time rather than
    # asked for twice; `page_type` is free text (pdp, plp, category, search,
    # article, ...) because the taxonomy will grow and a CHECK constraint would
    # turn each addition into a migration.
    op.execute("ALTER TABLE recipes ADD COLUMN domain TEXT")
    op.execute("ALTER TABLE recipes ADD COLUMN page_type TEXT")
    # Private until deliberately published: a tenant's recipe is their data,
    # and appearing in a shared catalogue has to be a choice, never a default.
    op.execute(
        "ALTER TABLE recipes ADD COLUMN template_visibility TEXT NOT NULL DEFAULT 'private'"
    )
    op.execute(
        "CREATE INDEX ix_recipes_marketplace ON recipes (template_visibility, domain, page_type)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_recipes_marketplace")
    op.execute("ALTER TABLE recipes DROP COLUMN IF EXISTS template_visibility")
    op.execute("ALTER TABLE recipes DROP COLUMN IF EXISTS page_type")
    op.execute("ALTER TABLE recipes DROP COLUMN IF EXISTS domain")
    op.execute("ALTER TABLE recipes DROP COLUMN IF EXISTS document")
    op.execute("ALTER TABLE recipe_versions DROP COLUMN IF EXISTS document")
