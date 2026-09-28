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


async def _audit_document(store: PostgresRecipeStore, recipe_id: str, version: int):
    """The `document` on a `recipe_versions` row.

    Read directly rather than through `list_versions`, which selects only
    `version`/`diff_summary`/`created_at` on purpose -- it backs a listing, and
    a recipe document runs to hundreds of lines, so returning fifty of them per
    page would be the wrong shape for that endpoint.
    """

    async with store._pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT document FROM recipe_versions WHERE recipe_id = %s AND version = %s",
                (recipe_id, version),
            )
            row = await cur.fetchone()
    return row[0] if row else None


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


async def test_apply_recipe_update_persists_the_v2_document(
    store: PostgresRecipeStore,
) -> None:
    """The wall between the agent and the marketplace.

    This method is the only path a build writes through, and for its whole life
    it wrote `global_setup`/`field_groups` and nothing else -- so every
    agent-built recipe had `document IS NULL`, and `_process_job_run` refuses
    exactly those: "this recipe has no v2 document, so it can only run against
    the URL pattern it was built for". The agent could not produce a recipe the
    marketplace would accept.
    """

    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    document = {
        "name": "n",
        "version": 1,
        "fields": {"price": {"type": {"kind": "scalar", "value_type": "price"}}},
        "field_groups": [
            {
                "group_id": "g0",
                "field_names": ["price"],
                "bindings": {
                    "price": [{"locator": {"kind": "css", "selector": "#p"}, "priority": 60}]
                },
            }
        ],
    }

    await store.apply_recipe_update(
        recipe.recipe_id, version=2, global_setup=[], field_groups=[],
        health_status="healthy", diff_summary="onboard", document=document,
    )

    fetched = await store.get_recipe(recipe.recipe_id, tenant)
    assert fetched is not None
    assert fetched.document == document

    # The audit row carries it too, or a rollback would restore a recipe with
    # its v2 half deleted -- which is why `0011` put the column on both tables.
    assert await _audit_document(store, recipe.recipe_id, 2) == document


async def test_apply_recipe_update_without_a_document_keeps_the_stored_one(
    store: PostgresRecipeStore,
) -> None:
    """A v1 heal only knows how to rewrite the v1 columns. If passing no
    document meant writing NULL, one heal of a studio-authored recipe would
    silently delete the half of it that the marketplace and the studio read --
    turning a working recipe into one that can only run against its own URL
    pattern, with nothing in the version history saying what happened."""

    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    document = {"name": "n", "version": 1, "fields": {}, "field_groups": []}
    await store.apply_recipe_update(
        recipe.recipe_id, version=2, global_setup=[], field_groups=[],
        health_status="healthy", document=document,
    )

    await store.apply_recipe_update(
        recipe.recipe_id, version=3, global_setup=[], field_groups=[],
        health_status="degraded", diff_summary="v1 heal",
    )

    fetched = await store.get_recipe(recipe.recipe_id, tenant)
    assert fetched is not None
    assert fetched.version == 3
    assert fetched.document == document

    # And the version row written by that heal carries it forward rather than
    # recording an apparent deletion.
    assert await _audit_document(store, recipe.recipe_id, 3) == document


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


# --- extraction jobs: a catalogue recipe applied to submitted urls ----------


async def test_create_job_queues_one_run_per_url(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    urls = ["https://x.test/1", "https://x.test/2", "https://x.test/3"]
    job_id, run_ids = await store.create_job(
        recipe_id=recipe.recipe_id, tenant=tenant, recipe_version=1, urls=urls,
    )
    assert len(run_ids) == 3

    job = await store.get_job(job_id, tenant)
    assert job is not None
    assert job.total == 3
    assert job.queued == 3
    assert job.status == "running"
    assert job.finished_at is None
    # The name is joined from the recipe rather than copied at submit time, so
    # a renamed recipe does not leave old jobs pointing at a name nobody uses.
    assert job.recipe_name == "n"

    runs = await store.list_job_runs(job_id, tenant)
    # Submission order, and stable across polls -- the runs share a timestamp
    # to the microsecond, so `created_at` alone is not a total order.
    assert [r.url for r in runs] == sorted(urls)
    assert all(r.kind == "replay" and r.job_id == job_id for r in runs)


async def test_a_job_run_carries_its_url_through_the_claim(store: PostgresRecipeStore) -> None:
    """The worker branches on `job_id`/`url`, so both have to survive claiming.

    Without them the run is indistinguishable from a scheduled replay, and
    would be executed against the recipe's own url_pattern by the v1 engine.
    """

    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    job_id, _ = await store.create_job(
        recipe_id=recipe.recipe_id, tenant=tenant, recipe_version=1,
        urls=["https://x.test/only"],
    )

    claimed = [c for c in await store.claim_runs_batch(10) if c.job_id == job_id]
    assert len(claimed) == 1
    assert claimed[0].url == "https://x.test/only"
    assert claimed[0].kind == "replay"


async def test_job_rollup_counts_and_finishes(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    job_id, _ = await store.create_job(
        recipe_id=recipe.recipe_id, tenant=tenant, recipe_version=1,
        urls=["https://x.test/1", "https://x.test/2"],
    )
    claimed = [c for c in await store.claim_runs_batch(50) if c.job_id == job_id]
    assert len(claimed) == 2

    await store.complete_run(
        claimed[0].run_id, claimed[0].lock, data={"price": "10"}, field_failures=None,
    )
    mid = await store.get_job(job_id, tenant)
    assert mid is not None
    # One done, one still running: not finished, and not yet a verdict.
    assert (mid.completed, mid.running) == (1, 1)
    assert mid.status == "running"
    assert mid.finished_at is None

    await store.fail_run(claimed[1].run_id, claimed[1].lock, "boom", max_attempts=0)
    done = await store.get_job(job_id, tenant)
    assert done is not None
    assert (done.completed, done.failed) == (1, 1)
    # Some yielded and some did not, which is a usable result -- not a failure.
    assert done.status == "partial"
    assert done.finished_at is not None


async def test_jobs_are_scoped_to_their_tenant(store: PostgresRecipeStore) -> None:
    tenant = _tenant()
    other = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    job_id, _ = await store.create_job(
        recipe_id=recipe.recipe_id, tenant=tenant, recipe_version=1,
        urls=["https://x.test/1"],
    )
    assert await store.get_job(job_id, other) is None
    assert await store.list_job_runs(job_id, other) == []
    assert await store.list_jobs(tenant=other) == []
    assert [j.job_id for j in await store.list_jobs(tenant=tenant)] == [job_id]


async def test_the_v2_document_reaches_the_worker(store: PostgresRecipeStore) -> None:
    """A job runs the v2 engine, which needs the document -- so `RecipeOut` has
    to carry it. It did not, and the v1 columns alone cannot express a
    `dom_rows` repeat or a field transform."""

    tenant = _tenant()
    document = {
        "name": "n",
        "target": {"match": []},
        "fields": {"price": {"type": {"kind": "scalar", "value_type": "price"}}},
        "field_groups": [{"group_id": "core", "field_names": ["price"], "bindings": {}}],
        "sample_urls": [],
    }
    recipe_id, _ = await store.save_document(tenant=tenant, document=document)

    fetched = await store.get_recipe(recipe_id, tenant)
    assert fetched is not None
    assert fetched.document is not None
    assert fetched.document["fields"]["price"]["type"]["value_type"] == "price"


async def test_defer_run_requeues_later_without_spending_an_attempt(
    store: PostgresRecipeStore,
) -> None:
    """A capacity refusal says nothing about the run, so it must not count.

    `fail_run` is the wrong tool twice: it requeues immediately, so three
    retries burn in about three seconds against a build that holds its browser
    for minutes, and it counts each one, so the run is terminally failed before
    the pressure it hit has had time to ease. Three Walgreens onboards died that
    way inside 0.1s of being claimed.
    """

    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    (claimed,) = await store.claim_runs_batch(10)
    await store.defer_run(run_id, claimed.lock, "node full", retry_after_seconds=300)

    fetched = await store.get_run(run_id, tenant)
    assert fetched is not None
    assert fetched.status == "queued"
    assert fetched.parked_until is not None

    # The attempt the claim spent is given back, so a run can be deferred
    # indefinitely without ever exhausting the budget that exists to stop a run
    # which keeps *breaking*.
    async with store._pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT attempts FROM recipe_runs WHERE run_id = %s", (run_id,))
        row = await cur.fetchone()
    assert row is not None and row[0] == 0


async def test_a_deferred_run_is_not_claimable_before_its_time(
    store: PostgresRecipeStore,
) -> None:
    """The delay has to be real, or deferring is just a slower spin.

    `claim_runs_batch` honours `parked_until` for queued rows now. Without that,
    the worker re-claims the run on its very next poll and meets the same full
    node, which is the busy-wait the old immediate requeue produced.
    """

    tenant = _tenant()
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema=SCHEMA,
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(recipe_id=recipe.recipe_id, tenant=tenant, kind="build")

    (claimed,) = await store.claim_runs_batch(10)
    await store.defer_run(run_id, claimed.lock, "node full", retry_after_seconds=300)

    assert [r.run_id for r in await store.claim_runs_batch(10) if r.run_id == run_id] == []

    # Once the park elapses it is ordinary queued work again.
    async with store._pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE recipe_runs SET parked_until = now() - INTERVAL '1 second' "
            "WHERE run_id = %s",
            (run_id,),
        )
    assert [r.run_id for r in await store.claim_runs_batch(10) if r.run_id == run_id] == [run_id]
