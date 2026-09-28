"""The claim loop must not be blocked by the work it claimed.

`tick` used to `await asyncio.gather(...)` over the whole claimed batch, making
every tick a barrier: `_run` could not come round again until the slowest run in
the batch finished. `max_concurrent` limited parallelism *inside* a tick and did
nothing across them.

Measured, and the reason this file exists: after the run tables were truncated,
two workers were each sitting inside `_await_assist` waiting on a row that no
longer existed, for the full 1800s `assist_timeout_s`. A freshly queued build
stayed `queued` and no worker touched it -- not for want of capacity
(`max_concurrent` is 3) but because neither worker's loop could come round
again.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentpilot.jobs.recipe_store import ClaimedRecipeRun, RecipeOut
from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop


def _claimed(run_id: str) -> ClaimedRecipeRun:
    recipe = RecipeOut(
        recipe_id="r1", tenant="acme", name="n", url_pattern="https://x.test/p/1",
        field_schema={}, version=1, global_setup=[], field_groups=[],
        health_status="healthy", last_verified_at=None, last_run_at=None,
        schedule_interval_seconds=None, created_at=None, updated_at=None,  # type: ignore[arg-type]
        document=None,
    )
    return ClaimedRecipeRun(
        run_id=run_id, recipe_id="r1", tenant="acme", kind="replay",
        params=None, lock=f"lk-{run_id}", recipe=recipe,
    )


class _Store:
    """Hands out queued runs and records how many were asked for."""

    def __init__(self, queued: int) -> None:
        self._queued = [f"run-{i}" for i in range(queued)]
        self.asked: list[int] = []

    async def reclaim_stale_runs(self, _after: float) -> None: ...
    async def reclaim_expired_parks(self) -> None: ...
    async def renew_lock(self, *_a: Any, **_k: Any) -> None: ...
    async def fail_run(self, *_a: Any, **_k: Any) -> None: ...

    async def claim_runs_batch(self, limit: int) -> list[ClaimedRecipeRun]:
        self.asked.append(limit)
        taken, self._queued = self._queued[:limit], self._queued[limit:]
        return [_claimed(r) for r in taken]


class _Loop(RecipeWorkerLoop):
    """Runs that block until released, instead of driving a browser."""

    def __init__(self, store: _Store, *, max_concurrent: int = 3) -> None:
        self._store = store  # type: ignore[assignment]
        self._batch_size = 5
        self._max_concurrent = max_concurrent
        self._poll_interval_seconds = 0.0
        self._stale_after_seconds = 120.0
        self._assist_poll_seconds = 0.0
        self._task = None
        self._inflight = set()
        self.started: list[str] = []
        self.release = asyncio.Event()

    async def _process_run(self, run: ClaimedRecipeRun) -> None:
        self.started.append(run.run_id)
        await self.release.wait()

    async def _heartbeat(self, run: ClaimedRecipeRun, deadline: float | None = None) -> None:
        # Mirrors the real signature, which now takes the run's wall-clock
        # deadline so the heartbeat can stop vouching for a wedged run. This
        # override exists only to keep the heartbeat out of the way while these
        # tests measure concurrency, so it ignores it.
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_a_tick_does_not_wait_for_the_work_it_claimed() -> None:
    """THE regression. A build is up to 15 agent steps and a parked run waits up
    to 1800s; if the tick waits for either, the worker claims nothing else for
    that whole time."""

    loop = _Loop(_Store(queued=1))

    await asyncio.wait_for(loop.tick(), timeout=1.0)   # must return immediately

    await asyncio.sleep(0)   # let the task start
    assert loop.started == ["run-0"], "the run never started"
    assert len(loop._inflight) == 1

    loop.release.set()
    await loop.drain()
    assert loop._inflight == set()


@pytest.mark.asyncio
async def test_a_later_tick_claims_while_an_earlier_run_is_still_going() -> None:
    """What the barrier cost: a second run sitting `queued` behind a first that
    had not finished."""

    store = _Store(queued=2)
    loop = _Loop(store)

    await loop.tick()
    await asyncio.sleep(0)
    await loop.tick()
    await asyncio.sleep(0)

    assert loop.started == ["run-0", "run-1"]

    loop.release.set()
    await loop.drain()


@pytest.mark.asyncio
async def test_no_more_than_max_concurrent_runs_are_claimed() -> None:
    """Capacity is enforced where the work is taken on rather than after it.

    Claiming a row locks it, so a worker that claims past its capacity keeps
    other workers off a run it has no room to start -- which is worse than
    leaving it `queued`.
    """

    store = _Store(queued=10)
    loop = _Loop(store, max_concurrent=3)

    for _ in range(4):
        await loop.tick()
        await asyncio.sleep(0)

    assert len(loop.started) == 3
    assert len(loop._inflight) == 3
    # A full worker does not query at all. The first tick asked for 3 -- its
    # whole capacity -- and the three that followed returned before touching the
    # store, so a saturated worker costs no `FOR UPDATE SKIP LOCKED` round trips
    # while it waits.
    assert store.asked == [3], store.asked

    loop.release.set()
    await loop.drain()


@pytest.mark.asyncio
async def test_capacity_is_released_as_each_run_ends_not_at_the_next_tick() -> None:
    store = _Store(queued=4)
    loop = _Loop(store, max_concurrent=1)

    await loop.tick()
    await asyncio.sleep(0)
    assert len(loop._inflight) == 1

    loop.release.set()
    await loop.drain()
    assert loop._inflight == set()

    loop.release = asyncio.Event()
    await loop.tick()
    await asyncio.sleep(0)
    assert loop.started == ["run-0", "run-1"]

    loop.release.set()
    await loop.drain()
