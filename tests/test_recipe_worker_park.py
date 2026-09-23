"""`RecipeWorkerLoop._await_assist` -- the park/poll/resume loop, against a
fake store. No Postgres, no browser.

The case these pin down is a race that silently threw away a person's work.
`submit_assist` sets `parked_until = NULL` **and** `status = 'running'` in one
statement, so a row that has just been answered reports no deadline at all. The
loop read the deadline first and fell back to the park deadline computed at
park time (`current.parked_until or deadline`); answering in the last poll
interval therefore broke out with `raw is None`, called `resume_run`, and
discarded answers the API had already accepted with a 200. Polling before
testing the clock is what separates "answered" from "expired".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop
from agentpilot.recipe.v2.assist import PendingAsk


@dataclass
class _Run:
    run_id: str = "run-1"
    lock: str = "lock-1"
    tenant: str = "t"


@dataclass
class _Row:
    """What `get_run` hands back -- only the two fields the loop reads."""

    status: str
    parked_until: datetime | None


@dataclass
class _FakeStore:
    """`park_run` always succeeds; `get_run`/`poll_assist` are scripted."""

    row: _Row
    answers: list[dict[str, Any]] | None = None
    resumed: list[str] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)

    async def park_run(self, run_id: str, lock: str, *, pending_asks: Any, parked_until: Any) -> bool:
        self.parked.append(run_id)
        return True

    async def get_run(self, run_id: str, tenant: str) -> _Row | None:
        return self.row

    async def poll_assist(self, run_id: str, lock: str) -> list[dict[str, Any]] | None:
        return self.answers

    async def resume_run(self, run_id: str, lock: str) -> None:
        self.resumed.append(run_id)


def _loop(store: Any) -> RecipeWorkerLoop:
    return RecipeWorkerLoop(
        store=store,
        registry=None,  # type: ignore[arg-type]
        driver=None,  # type: ignore[arg-type]
        profiles_root=None,  # type: ignore[arg-type]
        proxy_pinner=None,
        sessions={},
        placer=None,
        assist_poll_seconds=0.01,
    )


def _asks() -> list[PendingAsk]:
    return [PendingAsk(field="description", kind="rejected", reason="looked wrong")]


async def _await(loop: RecipeWorkerLoop, run: _Run, asks: list[PendingAsk], timeout_s: float):
    return await loop._await_assist(
        run,  # type: ignore[arg-type]
        object(),  # the recipe: untouched when no resolution acts on it
        asks,
        session=None,  # type: ignore[arg-type]
        url="https://example.com/p",
        llm_config=None,  # type: ignore[arg-type]
        timeout_s=timeout_s,
    )


async def test_an_answer_landing_after_the_original_deadline_is_not_discarded() -> None:
    """The regression. The row has been answered -- `status='running'`,
    `parked_until=NULL` -- and the park deadline has already passed. That
    combination used to read as expiry."""

    store = _FakeStore(row=_Row(status="running", parked_until=None), answers=[])
    loop = _loop(store)

    await _await(loop, _Run(), _asks(), timeout_s=0.0)

    assert store.resumed == [], "an answered run must not be resumed as if it had expired"


async def test_a_park_that_nobody_answered_still_expires() -> None:
    """The other half: a genuinely unanswered park resumes rather than holding
    the worker slot for ever."""

    past = datetime.now(UTC) - timedelta(seconds=5)
    store = _FakeStore(row=_Row(status="needs_input", parked_until=past), answers=None)
    loop = _loop(store)

    _recipe, unsettled = await _await(loop, _Run(), _asks(), timeout_s=0.0)

    assert store.resumed == ["run-1"]
    assert unsettled == {"description": "looked wrong"}


async def test_a_run_this_worker_no_longer_owns_is_released_not_waited_out() -> None:
    """`poll_assist` reads by `lock`, `get_run` by tenant. Seeing a row that has
    left `needs_input` which the lock cannot read means the run was reclaimed --
    waiting out the park would hold a browser and a warm identity for somebody
    else's run, and spinning on it would never end."""

    future = datetime.now(UTC) + timedelta(seconds=300)
    store = _FakeStore(row=_Row(status="running", parked_until=future), answers=None)
    loop = _loop(store)

    _recipe, unsettled = await _await(loop, _Run(), _asks(), timeout_s=300.0)

    assert store.resumed == []
    assert unsettled == {"description": "looked wrong"}
