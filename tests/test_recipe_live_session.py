"""The run's way back to its browser.

A recipe run drives a browser session, and the wizard held that session id in
React state alone -- not in the URL, not in storage, not derived from anything
the server knew. Reloading the tab lost it while the browser kept running, and
the orphaned session went on holding one of the node's few context slots until
something else reclaimed it.

Nothing had to be stored to fix that: the worker already names the session after
the run. These tests pin the two spellings together, because they are in
different processes and a silent divergence would give a client an id that
resolves to nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentpilot.gateway.routes.recipes import live_session_id_for
from agentpilot.jobs.recipe_store import RecipeRunOut


def _run(status: str) -> RecipeRunOut:
    return RecipeRunOut(
        run_id="11111111-2222-3333-4444-555555555555",
        recipe_id="r-1",
        tenant="t",
        kind="build",
        status=status,
        data=None,
        field_failures=None,
        error=None,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
    )


def test_the_id_matches_what_the_worker_actually_names_the_session() -> None:
    """`recipe_worker_loop._process_run` builds `f"recipe-run-{run.run_id}"`.

    Two processes spelling the same name from the same input is the whole
    mechanism -- there is no shared record to consult, which is precisely why it
    cannot go stale. It can, however, drift, so it is pinned here against the
    worker's own source rather than against a copy of the format string.
    """

    import inspect

    from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop

    source = inspect.getsource(RecipeWorkerLoop._process_run)
    assert 'f"recipe-run-{run.run_id}"' in source, (
        "the worker changed how it names live sessions; `live_session_id_for` "
        "derives the same string independently and must be changed with it"
    )
    assert live_session_id_for(_run("running")) == (
        "recipe-run-11111111-2222-3333-4444-555555555555"
    )


@pytest.mark.parametrize("status", ["running", "needs_input"])
def test_a_run_that_may_still_hold_a_browser_offers_it(status: str) -> None:
    """`needs_input` matters as much as `running`: a parked run is exactly the
    case where a person has to get back to the page, and it is the state a
    build sits in longest."""

    assert live_session_id_for(_run(status)) is not None


@pytest.mark.parametrize("status", ["queued", "completed", "failed", "cancelled"])
def test_a_run_with_no_browser_offers_nothing(status: str) -> None:
    """The id is derivable for any run id at all, so deriving it is not the
    question -- whether a browser is on the other end is. A finished run has
    released its session, and pointing a client at it would 404."""

    assert live_session_id_for(_run(status)) is None
