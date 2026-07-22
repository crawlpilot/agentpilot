"""create api_keys table

Revision ID: 0001
Revises:
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE api_keys (
            key_id         TEXT PRIMARY KEY,
            tenant          TEXT NOT NULL,
            name            TEXT NOT NULL,
            prefix          TEXT NOT NULL,
            digest          TEXT NOT NULL UNIQUE,
            created_at      TIMESTAMPTZ NOT NULL,
            last_used_at    TIMESTAMPTZ,
            revoked_at      TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX idx_api_keys_tenant ON api_keys (tenant)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_api_keys_tenant")
    op.execute("DROP TABLE IF EXISTS api_keys")
