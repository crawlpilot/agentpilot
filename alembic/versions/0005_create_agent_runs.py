"""create agent_runs, agent_steps

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same TEXT-PK / TEXT+CHECK-status house style as 0001/0002, and the
    # same lock/locked_at claim columns crawl_tasks uses -- but a run's lock
    # is renewed *periodically during* processing (a run can take minutes
    # across many steps), not just held for one quick unit like a crawl_task.
    op.execute(
        """
        CREATE TABLE agent_runs (
            run_id          TEXT PRIMARY KEY,
            tenant          TEXT NOT NULL,
            task            TEXT NOT NULL,
            domain          TEXT NOT NULL,
            tier            TEXT NOT NULL DEFAULT 'auto',
            output_schema   JSONB,
            max_steps       INT NOT NULL DEFAULT 50,
            status          TEXT NOT NULL
                            CHECK (status IN
                                ('queued', 'running', 'completed', 'failed', 'cancelled')),
            current_step    INT NOT NULL DEFAULT 0,
            result          JSONB,
            error           TEXT,
            lock            TEXT,
            locked_at       TIMESTAMPTZ,
            attempts        INT NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at      TIMESTAMPTZ,
            finished_at     TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_runs_claim ON agent_runs (status, created_at) "
        "WHERE status = 'queued'"
    )
    op.execute("CREATE INDEX idx_agent_runs_tenant ON agent_runs (tenant, created_at DESC)")

    # One row per step (unlike documents' one-row-per-URL) -- an agent run
    # needs the full turn-by-turn history for replay/debugging, not just the
    # final result.
    op.execute(
        """
        CREATE TABLE agent_steps (
            seq                      BIGSERIAL PRIMARY KEY,
            run_id                   TEXT NOT NULL REFERENCES agent_runs (run_id)
                                     ON DELETE CASCADE,
            step_number              INT NOT NULL,
            evaluation_previous_goal TEXT,
            memory                   TEXT,
            next_goal                TEXT,
            actions                  JSONB,
            action_results           JSONB,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_agent_steps_run_pagination ON agent_steps (run_id, seq)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_steps_run_pagination")
    op.execute("DROP TABLE IF EXISTS agent_steps")
    op.execute("DROP INDEX IF EXISTS idx_agent_runs_tenant")
    op.execute("DROP INDEX IF EXISTS idx_agent_runs_claim")
    op.execute("DROP TABLE IF EXISTS agent_runs")
