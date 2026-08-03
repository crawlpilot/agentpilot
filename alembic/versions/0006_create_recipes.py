"""create recipes, recipe_versions, recipe_runs

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `global_setup`/`field_groups` are JSONB-serialized
    # `RevealStep`/`FieldGroup` lists (agentpilot/recipe/models.py's
    # `to_dict()`/`from_dict()`) -- same "no ORM, raw JSONB" house style
    # `agent_runs.output_schema`/`agent_steps.actions` already use.
    op.execute(
        """
        CREATE TABLE recipes (
            recipe_id                  TEXT PRIMARY KEY,
            tenant                     TEXT NOT NULL,
            name                       TEXT NOT NULL,
            url_pattern                TEXT NOT NULL,
            field_schema               JSONB NOT NULL,
            version                    INT NOT NULL DEFAULT 1,
            global_setup               JSONB NOT NULL DEFAULT '[]',
            field_groups               JSONB NOT NULL DEFAULT '[]',
            health_status              TEXT NOT NULL DEFAULT 'healthy'
                                       CHECK (health_status IN ('healthy', 'degraded', 'broken')),
            last_verified_at           TIMESTAMPTZ,
            last_run_at                TIMESTAMPTZ,
            schedule_interval_seconds  DOUBLE PRECISION,
            next_due_at                TIMESTAMPTZ,
            created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_recipes_tenant ON recipes (tenant, created_at DESC)")
    op.execute(
        "CREATE INDEX idx_recipes_due ON recipes (next_due_at) "
        "WHERE schedule_interval_seconds IS NOT NULL"
    )

    # Append-only version history -- every build/heal writes a new row here
    # (old ones retained), never overwritten, so a redesign is auditable and
    # rollback-able.
    op.execute(
        """
        CREATE TABLE recipe_versions (
            seq            BIGSERIAL PRIMARY KEY,
            recipe_id      TEXT NOT NULL REFERENCES recipes (recipe_id) ON DELETE CASCADE,
            version        INT NOT NULL,
            global_setup   JSONB NOT NULL,
            field_groups   JSONB NOT NULL,
            diff_summary   TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_recipe_versions_recipe ON recipe_versions (recipe_id, version DESC)"
    )

    # One row per build/replay/heal/codegen execution -- same claim/lock
    # shape agent_runs uses, one shared queue for all four `kind`s.
    op.execute(
        """
        CREATE TABLE recipe_runs (
            run_id       TEXT PRIMARY KEY,
            recipe_id    TEXT REFERENCES recipes (recipe_id) ON DELETE CASCADE,
            tenant       TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK (kind IN ('build', 'replay', 'heal', 'codegen')),
            status       TEXT NOT NULL
                        CHECK (status IN
                            ('queued', 'running', 'completed', 'failed', 'cancelled')),
            params       JSONB,
            data         JSONB,
            field_failures JSONB,
            error        TEXT,
            lock         TEXT,
            locked_at    TIMESTAMPTZ,
            attempts     INT NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at   TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_recipe_runs_claim ON recipe_runs (status, created_at) "
        "WHERE status = 'queued'"
    )
    op.execute("CREATE INDEX idx_recipe_runs_tenant ON recipe_runs (tenant, created_at DESC)")
    op.execute("CREATE INDEX idx_recipe_runs_recipe ON recipe_runs (recipe_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_recipe_runs_recipe")
    op.execute("DROP INDEX IF EXISTS idx_recipe_runs_tenant")
    op.execute("DROP INDEX IF EXISTS idx_recipe_runs_claim")
    op.execute("DROP TABLE IF EXISTS recipe_runs")
    op.execute("DROP INDEX IF EXISTS idx_recipe_versions_recipe")
    op.execute("DROP TABLE IF EXISTS recipe_versions")
    op.execute("DROP INDEX IF EXISTS idx_recipes_due")
    op.execute("DROP INDEX IF EXISTS idx_recipes_tenant")
    op.execute("DROP TABLE IF EXISTS recipes")
