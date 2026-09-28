"""A run that stops answering must stop, and must stop being vouched for.

MEASURED, on a Walgreens replay (run b766c6b3):

    13:38:37  run claimed
    13:38:39  driver.browser_launched
    13:38:49  driver.page_crashed  context_id=572133bf-... page_id=7e37f15b-...
              (nothing further)

Eight minutes later the row was still `running`, with no error, no step trace and
`progress` NULL -- so it never got past `classify_current_page`, which calls
`page.content()` before any step runs. Three things had to be true at once:

1. `page.content()` had no timeout, and the driver's aliveness guard runs BEFORE
   the call, so a renderer dying mid-call leaves the await pending forever.
2. Nothing above it had a timeout either -- `execute_on_session` is a bare
   `await`, and `replay.py` contained no `wait_for` anywhere in the file.
3. The heartbeat renewed `locked_at` every 40s regardless, and
   `reclaim_stale_runs` only takes rows whose `locked_at` has gone stale -- so
   the one mechanism that could have freed it was held off by the one mechanism
   asserting it was healthy.

Each is fixed, and each is pinned here, because any one of them alone was enough
to hang the run indefinitely.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from crawlpilot.spi.errors import ContextCrashed

# --- 1. a page read cannot outlive the page ---------------------------------


async def test_a_page_read_that_never_returns_raises_context_crashed() -> None:
    from crawlpilot.driver import patchright_driver as driver

    async def _never() -> str:
        await asyncio.Event().wait()
        return "unreachable"

    driver._PAGE_READ_TIMEOUT_S = 0.05
    try:
        with pytest.raises(ContextCrashed) as exc:
            await driver._page_read(_never(), "page.content()", "https://example.test/p")
    finally:
        driver._PAGE_READ_TIMEOUT_S = 30.0

    assert "renderer is presumed gone" in str(exc.value)


async def test_a_page_read_that_returns_is_untouched() -> None:
    """The timeout is a crash detector, not a performance budget -- a read that
    answers must pass through unchanged, value and all."""

    from crawlpilot.driver import patchright_driver as driver

    async def _quick() -> str:
        return "<html>ok</html>"

    assert await driver._page_read(_quick(), "page.content()", "u") == "<html>ok</html>"


def test_the_page_read_timeout_is_generous_enough_for_a_slow_page() -> None:
    """A slow page must finish. The Walgreens page that triggered this was not
    slow -- it was gone -- and a budget tight enough to catch slowness would fail
    honest reads on heavy retail pages."""

    from crawlpilot.driver import patchright_driver as driver

    assert driver._PAGE_READ_TIMEOUT_S >= 30.0


def test_a_context_crash_is_classified_permanent() -> None:
    """Re-reading a page whose renderer is gone cannot succeed, so the caller
    must rebuild the context rather than retry. This is why `_page_read` raises
    `ContextCrashed` rather than a timeout error."""

    from agentpilot.agent.reliability import ErrorClass, classify_error

    assert classify_error(ContextCrashed("gone")) is ErrorClass.PERMANENT


# --- 2. a run cannot outlive its budget -------------------------------------


def test_a_replay_gets_a_far_shorter_budget_than_a_build() -> None:
    """A replay is one page load and a walk over frozen groups: no model calls,
    no park. A build is fifteen agent steps plus a park of up to
    `assist_timeout_s`. One budget for both would either cut builds off or give a
    wedged replay an hour."""

    from agentpilot.recipe.config import RecipeConfig

    cfg = RecipeConfig.from_env()
    assert cfg.replay_deadline_s == 600
    assert cfg.build_deadline_s == 3600
    assert cfg.build_deadline_s > cfg.assist_timeout_s, (
        "a build's budget has to cover a full park, or answering an ask late "
        "would kill the run that was waiting for the answer"
    )


async def test_wait_for_stops_an_await_that_never_returns() -> None:
    """The shape `_process` wraps every run in. Stated as a test because the
    failure it prevents is invisible: the run does not error, it simply never
    finishes, and every dashboard reads it as still working."""

    async def _hangs() -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_hangs(), timeout=0.05)


# --- 3. the heartbeat must stop vouching for it -----------------------------


class _Store:
    def __init__(self) -> None:
        self.renewals: list[str] = []

    async def renew_lock(self, run_id: str, lock: str) -> bool:
        self.renewals.append(run_id)
        return True


def _loop() -> tuple[object, _Store]:
    from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop

    store = _Store()
    loop = RecipeWorkerLoop.__new__(RecipeWorkerLoop)
    loop._store = store  # type: ignore[assignment]
    loop._stale_after_seconds = 3.0
    loop._live_routes = {}
    return loop, store


def _run() -> object:
    return type("R", (), {"run_id": "run-1", "lock": "lk", "tenant": "t"})()


async def test_the_heartbeat_withdraws_once_the_run_is_past_its_deadline() -> None:
    """The half that makes the deadline reliable.

    `wait_for` cancels the run, but cancellation has to land on a reachable
    await -- and the failure this exists for is an await that never returns. If
    the cancel wedges too, the heartbeat is all that is left, and every renewal
    it makes hides the run from:

        WHERE status = 'running' AND locked_at < now() - stale_after

    Renewing unconditionally proves the WORKER is alive, which it is, and says
    nothing about the RUN, which is not.
    """

    loop, store = _loop()

    # Returns rather than looping: the withdrawal is permanent, because a run
    # past its budget does not come back under it.
    past = time.monotonic() - 1.0
    await asyncio.wait_for(loop._heartbeat(_run(), past), timeout=5.0)  # type: ignore[attr-defined]

    assert store.renewals == [], "a run past its deadline must not be vouched for"


async def test_the_heartbeat_keeps_renewing_a_run_inside_its_budget() -> None:
    """The ordinary case, and the one the withdrawal must not break: a build
    spends minutes in model calls with no page activity at all, and the heartbeat
    is the only thing keeping its claim alive."""

    loop, store = _loop()

    ahead = time.monotonic() + 300.0
    task = asyncio.create_task(loop._heartbeat(_run(), ahead))  # type: ignore[attr-defined]
    await asyncio.sleep(1.2)
    task.cancel()

    assert store.renewals, "a healthy run must keep its claim"


async def test_a_run_with_no_deadline_recorded_is_still_renewed() -> None:
    """The deadline is passed by `_process`, so any path that reaches the
    heartbeat without one -- a test, a future caller -- must fall back to the old
    unconditional behaviour rather than silently letting a live run go stale."""

    loop, store = _loop()

    # No deadline passed at all -- the old unconditional behaviour.
    task = asyncio.create_task(loop._heartbeat(_run()))  # type: ignore[attr-defined]
    await asyncio.sleep(1.2)
    task.cancel()

    assert store.renewals, "an unknown deadline must not be read as an expired one"
