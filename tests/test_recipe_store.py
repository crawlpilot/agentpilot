"""`agentpilot.jobs.recipe_store.PostgresRecipeStore` -- against a real local
Postgres test database, same skip idiom as `tests/test_jobs_store.py`/
`tests/test_agent_store.py` (point `AGENTPILOT_TEST_DATABASE_URL` at a
scratch database with `alembic upgrade head` already applied; this suite is
skipped entirely otherwise).
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from agentpilot.jobs.recipe_store import PostgresRecipeStore

_DATABASE_URL = os.environ.get("AGENTPILOT_TEST_DATABASE_URL")


def _database_reachable() -> bool:
    if not _DATABASE_URL:
        return False
    try:
        with psycopg.connect(_DATABASE_URL, connect_timeout=1):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason="set AGENTPILOT_TEST_DATABASE_URL to a real local Postgres db with "
    "`alembic upgrade head` already applied",
)


@pytest.fixture
async def store():
    assert _DATABASE_URL is not None
    s = await PostgresRecipeStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM recipes WHERE tenant LIKE 'recipetest-%'")
    await s.close()


def _tenant() -> str:
    return f"recipetest-{uuid.uuid4().hex[:8]}"


SCHEMA = {"price": {"type": "scalar", "description": "the price"}}


async def test_create_and_get_recipe_round_trips(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant,
        name="test-recipe",
        url_pattern="https://example.test/p",
        field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    assert recipe.version == 0
    assert recipe.health_status == "degraded"

    fetched = await store.get_recipe(recipe.recipe_id, tenant)
    assert fetched is not None
    assert fetched.name == "test-recipe"
    assert fetched.field_schema == SCHEMA
    assert fetched.global_setup == []
    assert fetched.field_groups == []


async def test_get_recipe_returns_none_for_wrong_tenant(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    assert await store.get_recipe(recipe.recipe_id, "someone-else") is None


async def test_queue_run_and_get_run_round_trips(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    run = await store.get_run(run_id, tenant)
    assert run is not None
    assert run.kind == "build"
    assert run.status == "queued"
    assert run.recipe_id == recipe.recipe_id


async def test_queue_run_with_params_persists_them(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(
        recipe_id=recipe.recipe_id,
        tenant=tenant,
        kind="codegen",
        params={"language": "node-puppeteer"},
    )
    claimed = await store.claim_runs_batch(10)
    (this_run,) = [c for c in claimed if c.run_id == run_id]
    assert this_run.params == {"language": "node-puppeteer"}


async def test_claim_runs_batch_joins_the_recipe_row(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="joined", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    claimed = await store.claim_runs_batch(10)
    (this_run,) = [c for c in claimed if c.run_id == run_id]
    assert this_run.recipe.name == "joined"
    assert this_run.recipe.recipe_id == recipe.recipe_id

    fetched_run = await store.get_run(run_id, tenant)
    assert fetched_run is not None
    assert fetched_run.status == "running"


async def test_claim_runs_batch_does_not_reclaim_already_running(
    store: PostgresRecipeStore,
) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    first = await store.claim_runs_batch(10)
    assert any(c.run_id == run_id for c in first)
    second = await store.claim_runs_batch(10)
    assert not any(c.run_id == run_id for c in second)


async def test_renew_lock_only_succeeds_with_matching_lock(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")
    (claimed,) = await store.claim_runs_batch(10)

    assert await store.renew_lock(run_id, claimed.lock) is True
    assert await store.renew_lock(run_id, "wrong-lock") is False


async def test_reclaim_stale_runs_requeues_expired_lock(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")
    await store.claim_runs_batch(10)

    reclaimed = await store.reclaim_stale_runs(stale_after_seconds=0)
    assert reclaimed >= 1

    fetched = await store.get_run(run_id, tenant)
    assert fetched is not None
    assert fetched.status == "queued"


async def test_complete_run_sets_status_data_and_field_failures(
    store: PostgresRecipeStore,
) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="replay")
    (claimed,) = await store.claim_runs_batch(10)

    await store.complete_run(
        run_id, claimed.lock, data={"price": "9.99"}, field_failures={}, error=None
    )

    fetched = await store.get_run(run_id, tenant)
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.data == {"price": "9.99"}
    assert fetched.field_failures == {}
    assert fetched.finished_at is not None


async def test_fail_run_requeues_until_max_attempts_then_fails(
    store: PostgresRecipeStore,
) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    (claimed1,) = await store.claim_runs_batch(10)
    await store.fail_run(run_id, claimed1.lock, "boom", max_attempts=2)
    fetched = await store.get_run(run_id, tenant)
    assert fetched is not None
    assert fetched.status == "queued"

    (claimed2,) = await store.claim_runs_batch(10)
    await store.fail_run(run_id, claimed2.lock, "boom again", max_attempts=2)
    fetched = await store.get_run(run_id, tenant)
    assert fetched is not None
    assert fetched.status == "failed"
    assert fetched.error == "boom again"


async def test_cancel_run_only_from_queued_or_running(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    assert await store.cancel_run(run_id, tenant) is True
    fetched = await store.get_run(run_id, tenant)
    assert fetched is not None
    assert fetched.status == "cancelled"
    assert await store.cancel_run(run_id, tenant) is False


async def test_apply_recipe_update_bumps_version_and_writes_a_version_row(
    store: PostgresRecipeStore,
) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    field_groups = [
        {
            "group_id": "g0",
            "field_names": ["price"],
            "reveal_steps": [],
            "field_locators": {
                "price": {
                    "source": "css", "path": None, "selector": "#p", "attribute": "text",
                    "role": None, "name_contains": None,
                }
            },
            "repeat": None,
        }
    ]
    await store.apply_recipe_update(
        recipe.recipe_id, version=1, global_setup=[], field_groups=field_groups,
        health_status="healthy", diff_summary="initial build",
    )

    fetched = await store.get_recipe(recipe.recipe_id, tenant)
    assert fetched is not None
    assert fetched.version == 1
    assert fetched.health_status == "healthy"
    assert fetched.field_groups == field_groups

    versions = await store.list_versions(recipe.recipe_id, tenant)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["diff_summary"] == "initial build"


async def test_mark_replay_result_updates_health_and_timestamps(
    store: PostgresRecipeStore,
) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    await store.mark_replay_result(recipe.recipe_id, health_status="healthy")

    fetched = await store.get_recipe(recipe.recipe_id, tenant)
    assert fetched is not None
    assert fetched.health_status == "healthy"
    assert fetched.last_run_at is not None
    assert fetched.last_verified_at is not None


async def test_due_recipes_and_bump_next_due(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=0.0,
    )

    due = await store.due_recipes(50)
    due_ids = {d[0] for d in due}
    assert recipe.recipe_id in due_ids

    await store.bump_next_due(recipe.recipe_id, 3600.0)
    due_after_bump = await store.due_recipes(50)
    assert recipe.recipe_id not in {d[0] for d in due_after_bump}


async def test_due_recipes_excludes_on_demand_only_recipes(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    due = await store.due_recipes(50)
    assert recipe.recipe_id not in {d[0] for d in due}
