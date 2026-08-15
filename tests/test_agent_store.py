"""`agentpilot.jobs.agent_store.PostgresAgentStore` -- against a real local
Postgres test database, same skip idiom as `tests/test_jobs_store.py`
(point `AGENTPILOT_TEST_DATABASE_URL` at a scratch database with `alembic
upgrade head` already applied; this suite is skipped entirely otherwise).
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from agentpilot.agent.state import AgentStepRecord
from agentpilot.jobs.agent_store import PostgresAgentStore

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
    s = await PostgresAgentStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM agent_runs WHERE tenant LIKE 'agenttest-%'")
    await s.close()


def _tenant() -> str:
    return f"agenttest-{uuid.uuid4().hex[:8]}"


async def test_create_and_get_run_round_trips(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant,
        task="find the price",
        domain="example.com",
        tier="auto",
        output_schema=None,
        max_steps=10,
    )
    assert run.status == "queued"

    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.task == "find the price"
    assert fetched.status == "queued"
    assert fetched.current_step == 0


async def test_get_run_returns_none_for_wrong_tenant(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    assert await store.get_run(run.run_id, "someone-else") is None


async def test_claim_runs_batch_moves_queued_to_running(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )

    claimed = await store.claim_runs_batch(10)
    claimed_ids = {c.run_id for c in claimed}
    assert run.run_id in claimed_ids

    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.status == "running"


async def test_claim_runs_batch_does_not_reclaim_already_running(
    store: PostgresAgentStore,
) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    first = await store.claim_runs_batch(10)
    assert any(c.run_id == run.run_id for c in first)

    second = await store.claim_runs_batch(10)
    assert not any(c.run_id == run.run_id for c in second)


async def test_renew_lock_only_succeeds_with_matching_lock(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    (claimed,) = await store.claim_runs_batch(10)

    assert await store.renew_lock(run.run_id, claimed.lock) is True
    assert await store.renew_lock(run.run_id, "wrong-lock") is False


async def test_reclaim_stale_runs_requeues_expired_lock(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    await store.claim_runs_batch(10)

    reclaimed = await store.reclaim_stale_runs(stale_after_seconds=0)
    assert reclaimed >= 1

    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.status == "queued"


async def test_append_step_persists_and_bumps_current_step(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    await store.claim_runs_batch(10)  # claim before appending, per real usage

    step = AgentStepRecord(
        step_number=1,
        evaluation_previous_goal="starting",
        memory="",
        next_goal="click button",
        actions=[{"type": "click", "ref": "e1"}],
        action_results=["clicked"],
        thinking="the button is the obvious next control",
        duration_ms=1234,
        input_tokens=900,
        output_tokens=42,
        screenshot=b"\x89PNG\r\n\x1a\nfake",
    )
    await store.append_step(run.run_id, step)

    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.current_step == 1

    steps, next_cursor = await store.list_steps(run.run_id, tenant, after=None)
    assert len(steps) == 1
    assert steps[0].next_goal == "click button"
    assert steps[0].actions == [{"type": "click", "ref": "e1"}]
    assert steps[0].thinking == "the button is the obvious next control"
    assert steps[0].duration_ms == 1234
    assert steps[0].input_tokens == 900
    assert steps[0].output_tokens == 42
    # The list payload only reports presence, not the bytes ...
    assert steps[0].has_screenshot is True
    assert next_cursor is None

    # ... the bytes come back through the dedicated, tenant-scoped fetch.
    png = await store.get_step_screenshot(run.run_id, tenant, steps[0].seq)
    assert png == b"\x89PNG\r\n\x1a\nfake"
    assert await store.get_step_screenshot(run.run_id, "other-tenant", steps[0].seq) is None


async def test_complete_run_sets_status_and_result(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    (claimed,) = await store.claim_runs_batch(10)

    await store.complete_run(
        run.run_id, claimed.lock, {"success": True, "result": "done", "extracted_data": None}
    )

    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.result == {"success": True, "result": "done", "extracted_data": None}
    assert fetched.finished_at is not None


async def test_fail_run_requeues_until_max_attempts_then_fails(
    store: PostgresAgentStore,
) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )

    # Attempt 1: claim, fail -> requeued (attempts=1 < max_attempts=2).
    (claimed1,) = await store.claim_runs_batch(10)
    await store.fail_run(run.run_id, claimed1.lock, "boom", max_attempts=2)
    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.status == "queued"

    # Attempt 2: claim, fail -> permanently failed (attempts=2 >= max_attempts=2).
    (claimed2,) = await store.claim_runs_batch(10)
    await store.fail_run(run.run_id, claimed2.lock, "boom again", max_attempts=2)
    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.status == "failed"
    assert fetched.error == "boom again"


async def test_cancel_run_only_from_queued_or_running(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=5
    )
    assert await store.cancel_run(run.run_id, tenant) is True

    fetched = await store.get_run(run.run_id, tenant)
    assert fetched is not None
    assert fetched.status == "cancelled"

    # Already terminal -- cancelling again is a no-op (returns False).
    assert await store.cancel_run(run.run_id, tenant) is False


async def test_list_steps_paginates_with_keyset_cursor(store: PostgresAgentStore) -> None:
    tenant = _tenant()
    run = await store.create_run(
        tenant=tenant, task="t", domain="d", tier="auto", output_schema=None, max_steps=20
    )
    await store.claim_runs_batch(10)

    for i in range(1, 6):
        await store.append_step(
            run.run_id,
            AgentStepRecord(
                step_number=i,
                evaluation_previous_goal="",
                memory="",
                next_goal=f"goal-{i}",
                actions=[],
                action_results=[],
            ),
        )

    first_page, cursor = await store.list_steps(run.run_id, tenant, after=None, limit=3)
    assert len(first_page) == 3
    assert cursor is not None

    second_page, cursor2 = await store.list_steps(run.run_id, tenant, after=cursor, limit=3)
    assert len(second_page) == 2
    assert cursor2 is None
    assert [s.next_goal for s in first_page + second_page] == [f"goal-{i}" for i in range(1, 6)]
