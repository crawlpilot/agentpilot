"""Postgres-backed queue for agent runs (`/v1/agent/runs`) -- same
`SELECT ... FOR UPDATE SKIP LOCKED` claim/lock/retry shape `jobs/store.py`'s
`PostgresJobStore` already uses for `crawl_tasks`, in a separate store/table
pair rather than an extension of it: an agent run needs full turn-by-turn
step history (`agent_steps`, one row per step, not one row per URL like
`documents`), and its lock is renewed *periodically during* processing (a
run can take minutes across many steps) rather than held for one quick unit
like a crawl task -- different enough shape to warrant its own tables.

Schema: `alembic/versions/0005_create_agent_runs.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from agentpilot.agent.state import AgentStepRecord


@dataclass
class AgentRun:
    """The tenant-facing polling shape (`GET /v1/agent/runs/{id}`)."""

    run_id: str
    tenant: str
    task: str
    status: str
    current_step: int
    max_steps: int
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass
class ClaimedAgentRun:
    """One row `claim_runs_batch` moved `queued -> running` and locked."""

    run_id: str
    tenant: str
    task: str
    domain: str
    tier: str
    output_schema: dict[str, Any] | None
    max_steps: int
    attempts: int
    lock: str


@dataclass
class AgentStepOut:
    seq: int
    step_number: int
    evaluation_previous_goal: str | None
    memory: str | None
    next_goal: str | None
    actions: list[dict[str, Any]]
    action_results: list[str]
    thinking: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    has_screenshot: bool
    created_at: datetime


def _run_from_row(row: dict[str, Any]) -> AgentRun:
    return AgentRun(
        run_id=row["run_id"],
        tenant=row["tenant"],
        task=row["task"],
        status=row["status"],
        current_step=row["current_step"],
        max_steps=row["max_steps"],
        result=row["result"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class PostgresAgentStore:
    """Raw `psycopg` SQL against `agent_runs`/`agent_steps`, matching
    `PostgresJobStore`'s shape exactly (async `connect()` factory,
    `dict_row`, no ORM)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresAgentStore:
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            database_url, min_size=1, max_size=10, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True, timeout=10.0)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def create_run(
        self,
        *,
        tenant: str,
        task: str,
        domain: str,
        tier: str,
        output_schema: dict[str, Any] | None,
        max_steps: int,
    ) -> AgentRun:
        from psycopg.types.json import Jsonb

        run_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runs (
                    run_id, tenant, task, domain, tier, output_schema, max_steps,
                    status, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                """,
                (
                    run_id,
                    tenant,
                    task,
                    domain,
                    tier,
                    Jsonb(output_schema) if output_schema is not None else None,
                    max_steps,
                    created_at,
                ),
            )
        return AgentRun(
            run_id=run_id,
            tenant=tenant,
            task=task,
            status="queued",
            current_step=0,
            max_steps=max_steps,
            result=None,
            error=None,
            created_at=created_at,
            started_at=None,
            finished_at=None,
        )

    async def list_runs(
        self, tenant: str, after: str | None, limit: int = 50
    ) -> tuple[list[AgentRun], str | None]:
        """Keyset-paginated by `created_at DESC` over `idx_agent_runs_tenant
        (tenant, created_at DESC)` -- same shape as `recipe_store.list_recipes`.
        `after` is the previous page's last row's `created_at.isoformat()`."""

        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                params: tuple[Any, ...] = (tenant,)
                cursor_clause = ""
                if after is not None:
                    cursor_clause = "AND created_at < %s"
                    params += (after,)
                await cur.execute(
                    f"""
                    SELECT run_id, tenant, task, status, current_step, max_steps, result,
                           error, created_at, started_at, finished_at
                    FROM agent_runs
                    WHERE tenant = %s {cursor_clause}
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (*params, limit),
                )
                rows = await cur.fetchall()
        runs = [_run_from_row(r) for r in rows]
        next_cursor = runs[-1].created_at.isoformat() if len(runs) == limit else None
        return runs, next_cursor

    async def get_run(self, run_id: str, tenant: str) -> AgentRun | None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT run_id, tenant, task, status, current_step, max_steps, result, "
                    "error, created_at, started_at, finished_at "
                    "FROM agent_runs WHERE run_id = %s AND tenant = %s",
                    (run_id, tenant),
                )
                row = await cur.fetchone()
        return _run_from_row(row) if row else None

    async def cancel_run(self, run_id: str, tenant: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = 'cancelled', finished_at = %s "
                    "WHERE run_id = %s AND tenant = %s AND status IN ('queued', 'running')",
                    (datetime.now(UTC), run_id, tenant),
                )
                return cur.rowcount > 0

    async def claim_runs_batch(self, limit: int) -> list[ClaimedAgentRun]:
        from psycopg.rows import dict_row

        lock = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH next AS (
                        SELECT run_id FROM agent_runs
                        WHERE status = 'queued'
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE agent_runs r
                    SET status = 'running', lock = %s, locked_at = now(),
                        attempts = attempts + 1, started_at = now()
                    FROM next
                    WHERE r.run_id = next.run_id
                    RETURNING r.run_id, r.tenant, r.task, r.domain, r.tier,
                              r.output_schema, r.max_steps, r.attempts
                    """,
                    (limit, lock),
                )
                rows = await cur.fetchall()
        return [
            ClaimedAgentRun(
                run_id=r["run_id"],
                tenant=r["tenant"],
                task=r["task"],
                domain=r["domain"],
                tier=r["tier"],
                output_schema=r["output_schema"],
                max_steps=r["max_steps"],
                attempts=r["attempts"],
                lock=lock,
            )
            for r in rows
        ]

    async def renew_lock(self, run_id: str, lock: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET locked_at = now() "
                    "WHERE run_id = %s AND lock = %s AND status = 'running'",
                    (run_id, lock),
                )
                return cur.rowcount > 0

    async def reclaim_stale_runs(self, stale_after_seconds: float) -> int:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = 'queued', lock = NULL, locked_at = NULL "
                    "WHERE status = 'running' AND locked_at < now() - %s * INTERVAL '1 second'",
                    (stale_after_seconds,),
                )
                return cur.rowcount

    async def append_step(self, run_id: str, step: AgentStepRecord) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_steps (
                    run_id, step_number, evaluation_previous_goal, memory, next_goal,
                    actions, action_results, thinking, duration_ms, input_tokens,
                    output_tokens, screenshot
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    step.step_number,
                    step.evaluation_previous_goal,
                    step.memory,
                    step.next_goal,
                    Jsonb(step.actions),
                    Jsonb(step.action_results),
                    step.thinking,
                    step.duration_ms,
                    step.input_tokens,
                    step.output_tokens,
                    step.screenshot,
                ),
            )
            await conn.execute(
                "UPDATE agent_runs SET current_step = %s WHERE run_id = %s",
                (step.step_number, run_id),
            )

    async def complete_run(self, run_id: str, lock: str, result: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_runs SET status = 'completed', result = %s, finished_at = now() "
                "WHERE run_id = %s AND lock = %s",
                (Jsonb(result), run_id, lock),
            )

    async def fail_run(
        self, run_id: str, lock: str, error: str, max_attempts: int = 3
    ) -> None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT attempts FROM agent_runs WHERE run_id = %s AND lock = %s "
                    "AND status = 'running'",
                    (run_id, lock),
                )
                row = await cur.fetchone()
                if row is None:
                    return  # already reclaimed by reclaim_stale_runs
                if row["attempts"] < max_attempts:
                    await cur.execute(
                        "UPDATE agent_runs SET status = 'queued', lock = NULL, locked_at = NULL, "
                        "error = %s WHERE run_id = %s AND lock = %s",
                        (error, run_id, lock),
                    )
                    return
                await cur.execute(
                    "UPDATE agent_runs SET status = 'failed', finished_at = now(), error = %s "
                    "WHERE run_id = %s AND lock = %s",
                    (error, run_id, lock),
                )

    async def list_steps(
        self, run_id: str, tenant: str, after: str | None, limit: int = 100
    ) -> tuple[list[AgentStepOut], str | None]:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                params: tuple[Any, ...] = (run_id, tenant)
                cursor_clause = ""
                if after is not None:
                    cursor_clause = "AND s.seq > %s"
                    params += (int(after),)
                # `screenshot IS NOT NULL` (not the bytes themselves) so the
                # step list stays a light JSON payload -- the image is fetched
                # lazily per step via `get_step_screenshot`.
                await cur.execute(
                    f"""
                    SELECT s.seq, s.step_number, s.evaluation_previous_goal, s.memory,
                           s.next_goal, s.actions, s.action_results, s.thinking,
                           s.duration_ms, s.input_tokens, s.output_tokens,
                           (s.screenshot IS NOT NULL) AS has_screenshot, s.created_at
                    FROM agent_steps s JOIN agent_runs r ON r.run_id = s.run_id
                    WHERE s.run_id = %s AND r.tenant = %s {cursor_clause}
                    ORDER BY s.seq LIMIT %s
                    """,
                    (*params, limit),
                )
                rows = await cur.fetchall()
        steps = [
            AgentStepOut(
                seq=r["seq"],
                step_number=r["step_number"],
                evaluation_previous_goal=r["evaluation_previous_goal"],
                memory=r["memory"],
                next_goal=r["next_goal"],
                actions=r["actions"] or [],
                action_results=r["action_results"] or [],
                thinking=r["thinking"],
                duration_ms=r["duration_ms"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                has_screenshot=r["has_screenshot"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
        next_cursor = str(rows[-1]["seq"]) if len(rows) == limit else None
        return steps, next_cursor

    async def get_step_screenshot(self, run_id: str, tenant: str, seq: int) -> bytes | None:
        """The raw PNG for one step, tenant-scoped (joins `agent_runs` so a
        tenant can't read another's screenshot). `None` when the step has no
        screenshot or doesn't belong to this tenant's run."""

        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT s.screenshot FROM agent_steps s "
                    "JOIN agent_runs r ON r.run_id = s.run_id "
                    "WHERE s.run_id = %s AND r.tenant = %s AND s.seq = %s",
                    (run_id, tenant, seq),
                )
                row = await cur.fetchone()
        if row is None or row["screenshot"] is None:
            return None
        return bytes(row["screenshot"])
