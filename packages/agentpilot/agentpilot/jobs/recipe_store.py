"""Postgres-backed persistence for `agentpilot.recipe` -- `recipes` (current
state), `recipe_versions` (append-only build/heal history), and
`recipe_runs` (one shared `FOR UPDATE SKIP LOCKED` queue for all four stage
kinds: `build`/`replay`/`heal`/`codegen`), same claim/lock/retry shape
`jobs/agent_store.py`'s `PostgresAgentStore` already uses.

Schema: `alembic/versions/0006_create_recipes.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool


@dataclass
class RecipeOut:
    recipe_id: str
    tenant: str
    name: str
    url_pattern: str
    field_schema: dict[str, Any]
    version: int
    global_setup: list[dict[str, Any]]
    field_groups: list[dict[str, Any]]
    health_status: str
    last_verified_at: datetime | None
    last_run_at: datetime | None
    schedule_interval_seconds: float | None
    created_at: datetime
    updated_at: datetime
    heal_attempts: int = 0


@dataclass
class ClaimedRecipeRun:
    run_id: str
    recipe_id: str
    tenant: str
    kind: str
    params: dict[str, Any] | None
    lock: str
    recipe: RecipeOut


@dataclass
class RecipeRunOut:
    run_id: str
    recipe_id: str
    tenant: str
    kind: str
    status: str
    data: dict[str, Any] | None
    field_failures: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _recipe_from_row(row: dict[str, Any]) -> RecipeOut:
    return RecipeOut(
        recipe_id=row["recipe_id"],
        tenant=row["tenant"],
        name=row["name"],
        url_pattern=row["url_pattern"],
        field_schema=row["field_schema"],
        version=row["version"],
        global_setup=row["global_setup"] or [],
        field_groups=row["field_groups"] or [],
        health_status=row["health_status"],
        last_verified_at=row["last_verified_at"],
        last_run_at=row["last_run_at"],
        schedule_interval_seconds=row["schedule_interval_seconds"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        heal_attempts=row.get("heal_attempts", 0),
    )


def _run_from_row(row: dict[str, Any]) -> RecipeRunOut:
    return RecipeRunOut(
        run_id=row["run_id"],
        recipe_id=row["recipe_id"],
        tenant=row["tenant"],
        kind=row["kind"],
        status=row["status"],
        data=row["data"],
        field_failures=row["field_failures"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


_RECIPE_COLUMNS = (
    "recipe_id, tenant, name, url_pattern, field_schema, version, global_setup, "
    "field_groups, health_status, last_verified_at, last_run_at, "
    "schedule_interval_seconds, created_at, updated_at, heal_attempts"
)

_RUN_COLUMNS = (
    "run_id, recipe_id, tenant, kind, status, data, field_failures, error, "
    "created_at, started_at, finished_at"
)


class PostgresRecipeStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresRecipeStore:
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            database_url, min_size=1, max_size=10, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True, timeout=10.0)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def create_recipe(
        self,
        *,
        tenant: str,
        name: str,
        url_pattern: str,
        field_schema: dict[str, Any],
        schedule_interval_seconds: float | None,
    ) -> RecipeOut:
        from psycopg.types.json import Jsonb

        recipe_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO recipes (
                    recipe_id, tenant, name, url_pattern, field_schema, version,
                    global_setup, field_groups, health_status,
                    schedule_interval_seconds, next_due_at, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, 0, '[]', '[]', 'degraded', %s, %s, %s, %s)
                """,
                (
                    recipe_id,
                    tenant,
                    name,
                    url_pattern,
                    Jsonb(field_schema),
                    schedule_interval_seconds,
                    _next_due_at(now, schedule_interval_seconds),
                    now,
                    now,
                ),
            )
        return RecipeOut(
            recipe_id=recipe_id,
            tenant=tenant,
            name=name,
            url_pattern=url_pattern,
            field_schema=field_schema,
            version=0,
            global_setup=[],
            field_groups=[],
            health_status="degraded",
            last_verified_at=None,
            last_run_at=None,
            schedule_interval_seconds=schedule_interval_seconds,
            created_at=now,
            updated_at=now,
        )

    async def get_recipe(self, recipe_id: str, tenant: str) -> RecipeOut | None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT {_RECIPE_COLUMNS} FROM recipes WHERE recipe_id = %s AND tenant = %s",
                    (recipe_id, tenant),
                )
                row = await cur.fetchone()
        return _recipe_from_row(row) if row else None

    async def list_recipes(
        self, tenant: str, after: str | None, limit: int = 50
    ) -> tuple[list[RecipeOut], str | None]:
        """Keyset-paginated by `created_at DESC`, using the existing
        `idx_recipes_tenant (tenant, created_at DESC)` index -- same shape
        as `list_versions`/`agent_store.list_steps`. `after` is the
        previous page's last row's `created_at.isoformat()`."""

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
                    SELECT {_RECIPE_COLUMNS} FROM recipes
                    WHERE tenant = %s {cursor_clause}
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (*params, limit),
                )
                rows = await cur.fetchall()
        recipes = [_recipe_from_row(r) for r in rows]
        next_cursor = recipes[-1].created_at.isoformat() if len(recipes) == limit else None
        return recipes, next_cursor

    async def list_versions(
        self, recipe_id: str, tenant: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT v.version, v.diff_summary, v.created_at
                    FROM recipe_versions v JOIN recipes r ON r.recipe_id = v.recipe_id
                    WHERE v.recipe_id = %s AND r.tenant = %s
                    ORDER BY v.version DESC LIMIT %s
                    """,
                    (recipe_id, tenant, limit),
                )
                rows = await cur.fetchall()
        return list(rows)

    async def queue_run(
        self, *, recipe_id: str, tenant: str, kind: str, params: dict[str, Any] | None = None
    ) -> str:
        from psycopg.types.json import Jsonb

        run_id = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO recipe_runs
                    (run_id, recipe_id, tenant, kind, status, params, created_at)
                VALUES (%s, %s, %s, %s, 'queued', %s, %s)
                """,
                (
                    run_id,
                    recipe_id,
                    tenant,
                    kind,
                    Jsonb(params) if params else None,
                    datetime.now(UTC),
                ),
            )
        return run_id

    async def get_run(self, run_id: str, tenant: str) -> RecipeRunOut | None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT {_RUN_COLUMNS} FROM recipe_runs WHERE run_id = %s AND tenant = %s",
                    (run_id, tenant),
                )
                row = await cur.fetchone()
        return _run_from_row(row) if row else None

    async def cancel_run(self, run_id: str, tenant: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE recipe_runs SET status = 'cancelled', finished_at = %s "
                    "WHERE run_id = %s AND tenant = %s AND status IN ('queued', 'running')",
                    (datetime.now(UTC), run_id, tenant),
                )
                return cur.rowcount > 0

    async def claim_runs_batch(self, limit: int) -> list[ClaimedRecipeRun]:
        from psycopg.rows import dict_row

        lock = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH next AS (
                        SELECT run_id FROM recipe_runs
                        WHERE status = 'queued'
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE recipe_runs r
                    SET status = 'running', lock = %s, locked_at = now(),
                        attempts = attempts + 1, started_at = now()
                    FROM next
                    WHERE r.run_id = next.run_id
                    RETURNING r.run_id, r.recipe_id, r.tenant, r.kind, r.params
                    """,
                    (limit, lock),
                )
                run_rows = await cur.fetchall()
                if not run_rows:
                    return []

                recipe_ids = list({r["recipe_id"] for r in run_rows})
                await cur.execute(
                    f"SELECT {_RECIPE_COLUMNS} FROM recipes WHERE recipe_id = ANY(%s)",
                    (recipe_ids,),
                )
                recipe_rows = await cur.fetchall()
                recipes_by_id = {row["recipe_id"]: _recipe_from_row(row) for row in recipe_rows}

        claimed: list[ClaimedRecipeRun] = []
        for r in run_rows:
            recipe = recipes_by_id.get(r["recipe_id"])
            if recipe is None:
                continue  # recipe deleted out from under a queued run -- skip, not crash
            claimed.append(
                ClaimedRecipeRun(
                    run_id=r["run_id"],
                    recipe_id=r["recipe_id"],
                    tenant=r["tenant"],
                    kind=r["kind"],
                    params=r["params"],
                    lock=lock,
                    recipe=recipe,
                )
            )
        return claimed

    async def renew_lock(self, run_id: str, lock: str) -> bool:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE recipe_runs SET locked_at = now() "
                    "WHERE run_id = %s AND lock = %s AND status = 'running'",
                    (run_id, lock),
                )
                return cur.rowcount > 0

    async def reclaim_stale_runs(self, stale_after_seconds: float) -> int:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE recipe_runs SET status = 'queued', lock = NULL, locked_at = NULL "
                    "WHERE status = 'running' AND locked_at < now() - %s * INTERVAL '1 second'",
                    (stale_after_seconds,),
                )
                return cur.rowcount

    async def complete_run(
        self,
        run_id: str,
        lock: str,
        *,
        data: dict[str, Any] | None,
        field_failures: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE recipe_runs SET status = 'completed', data = %s, field_failures = %s, "
                "error = %s, finished_at = now() WHERE run_id = %s AND lock = %s",
                (
                    Jsonb(data) if data is not None else None,
                    Jsonb(field_failures) if field_failures is not None else None,
                    error,
                    run_id,
                    lock,
                ),
            )

    async def fail_run(self, run_id: str, lock: str, error: str, max_attempts: int = 3) -> None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT attempts FROM recipe_runs WHERE run_id = %s AND lock = %s "
                    "AND status = 'running'",
                    (run_id, lock),
                )
                row = await cur.fetchone()
                if row is None:
                    return
                if row["attempts"] < max_attempts:
                    await cur.execute(
                        "UPDATE recipe_runs SET status = 'queued', lock = NULL, locked_at = NULL, "
                        "error = %s WHERE run_id = %s AND lock = %s",
                        (error, run_id, lock),
                    )
                    return
                await cur.execute(
                    "UPDATE recipe_runs SET status = 'failed', finished_at = now(), error = %s "
                    "WHERE run_id = %s AND lock = %s",
                    (error, run_id, lock),
                )

    async def apply_recipe_update(
        self,
        recipe_id: str,
        *,
        version: int,
        global_setup: list[dict[str, Any]],
        field_groups: list[dict[str, Any]],
        health_status: str,
        diff_summary: str | None = None,
        heal_attempts: str = "keep",
    ) -> None:
        """Called by the worker after a `build`/`heal` run completes --
        updates the current recipe row and appends an audit entry to
        `recipe_versions`, in one transaction.

        `last_verified_at` advances ONLY when the result is `healthy` (a
        degraded heal must not overstate freshness). `heal_attempts` is
        `reset` (0), `increment` (+1), or `keep` -- the worker resets on build
        / healthy heal and increments on a heal that didn't restore health, so
        the streak drives the `max_heal_attempts` cutoff."""

        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE recipes SET version = %s, global_setup = %s, field_groups = %s, "
                "health_status = %s, updated_at = now(), "
                "last_verified_at = CASE WHEN %s = 'healthy' THEN now() ELSE last_verified_at END, "
                "heal_attempts = CASE WHEN %s = 'reset' THEN 0 "
                "WHEN %s = 'increment' THEN heal_attempts + 1 ELSE heal_attempts END "
                "WHERE recipe_id = %s",
                (
                    version,
                    Jsonb(global_setup),
                    Jsonb(field_groups),
                    health_status,
                    health_status,
                    heal_attempts,
                    heal_attempts,
                    recipe_id,
                ),
            )
            await conn.execute(
                "INSERT INTO recipe_versions "
                "(recipe_id, version, global_setup, field_groups, diff_summary) "
                "VALUES (%s, %s, %s, %s, %s)",
                (recipe_id, version, Jsonb(global_setup), Jsonb(field_groups), diff_summary),
            )

    async def mark_replay_result(self, recipe_id: str, *, health_status: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE recipes SET health_status = %s, last_run_at = now(), "
                "last_verified_at = CASE WHEN %s = 'healthy' THEN now() ELSE last_verified_at END, "
                # A healthy scheduled replay clears the failed-heal streak.
                "heal_attempts = CASE WHEN %s = 'healthy' THEN 0 ELSE heal_attempts END, "
                "updated_at = now() WHERE recipe_id = %s",
                (health_status, health_status, health_status, recipe_id),
            )

    async def mark_recipe_broken(self, recipe_id: str) -> None:
        """Give up auto-healing: flip health to `broken` without a version
        bump or reveal-step change -- used when a recipe exhausts
        `max_heal_attempts`. Only a fresh `build` (which resets heal_attempts)
        gets it out of this state."""

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE recipes SET health_status = 'broken', updated_at = now() "
                "WHERE recipe_id = %s",
                (recipe_id,),
            )

    async def due_recipes(self, limit: int) -> list[tuple[str, str, float]]:
        """`(recipe_id, tenant, schedule_interval_seconds)` for every recipe
        whose schedule is due -- `recipe_scheduler_loop.py` enqueues a
        `replay` run for each and calls `bump_next_due`. Returns tenant
        alongside recipe_id since the scheduler is an internal, cross-tenant
        loop (not a tenant-scoped HTTP request) and `queue_run` needs it."""

        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT recipe_id, tenant, schedule_interval_seconds FROM recipes "
                    "WHERE schedule_interval_seconds IS NOT NULL AND next_due_at <= now() "
                    "ORDER BY next_due_at ASC LIMIT %s",
                    (limit,),
                )
                rows = await cur.fetchall()
        return [(r["recipe_id"], r["tenant"], r["schedule_interval_seconds"]) for r in rows]

    async def bump_next_due(self, recipe_id: str, interval_seconds: float) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE recipes SET next_due_at = now() + %s * INTERVAL '1 second' "
                "WHERE recipe_id = %s",
                (interval_seconds, recipe_id),
            )


def _next_due_at(now: datetime, schedule_interval_seconds: float | None) -> datetime | None:
    if schedule_interval_seconds is None:
        return None
    from datetime import timedelta

    return now + timedelta(seconds=schedule_interval_seconds)
