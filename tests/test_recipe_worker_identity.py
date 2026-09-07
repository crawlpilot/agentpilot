"""Which browser identity a recipe run opens against.

This is an anti-detection concern, not bookkeeping. The identity is
`{tenant}/{domain}/{name}`, and `name` decides whether the run gets a warm
profile or a brand-new one. The loop used to pass `recipe-run-{run_id}` --
unique per run -- so every run opened a cookie-less, history-less, first-visit
Chrome behind a freshly picked proxy, which `session/ephemeral.py` calls out as
"itself a bot signal to WAFs like Akamai". Walmart and Zara run exactly that.

Only the identity choice is exercised here -- `open_interactive_session` is
faked. The real thing needs a browser and lives in `tests/driver_contract/`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentpilot.jobs import recipe_worker_loop as loop_mod
from agentpilot.jobs.recipe_store import ClaimedRecipeRun, RecipeOut
from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop
from crawlpilot.spi.errors import LeaseConflict


def _run(run_id: str) -> ClaimedRecipeRun:
    recipe = RecipeOut(
        recipe_id="r1", tenant="acme", name="n", url_pattern="",
        field_schema={}, version=1, global_setup=[], field_groups=[],
        health_status="healthy", last_verified_at=None, last_run_at=None,
        schedule_interval_seconds=None, created_at=None, updated_at=None,  # type: ignore[arg-type]
    )
    return ClaimedRecipeRun(
        run_id=run_id, recipe_id="r1", tenant="acme", kind="replay",
        params=None, lock="lk", recipe=recipe,
    )


def _loop() -> RecipeWorkerLoop:
    return RecipeWorkerLoop(
        store=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        driver=None,  # type: ignore[arg-type]
        profiles_root=None,  # type: ignore[arg-type]
        proxy_pinner=None,
    )


class _Opener:
    """Records the identity names asked for, and can refuse a set of them the
    way `Registry.acquire` refuses an identity that is already leased."""

    def __init__(self, busy: set[str] | None = None) -> None:
        self.asked: list[str] = []
        self.session_ids: list[str] = []
        self._busy = busy or set()

    async def __call__(self, **kwargs: Any) -> str:
        self.asked.append(kwargs["name"])
        self.session_ids.append(kwargs["session_id"])
        if kwargs["name"] in self._busy:
            raise LeaseConflict("already leased")
        return f"session:{kwargs['name']}"


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch):
    def _install(busy: set[str] | None = None) -> _Opener:
        o = _Opener(busy)
        monkeypatch.setattr(loop_mod, "open_interactive_session", o)
        return o
    return _install


@pytest.mark.asyncio
async def test_a_run_opens_on_a_reusable_identity(opener) -> None:
    """The whole point: a name that another run can warm up for this one."""

    o = opener()
    session = await _loop()._open_warm_session(_run("run-a"), "www.walmart.com")

    assert len(o.asked) == 1
    name = o.asked[0]
    assert name.startswith("recipe-")
    # The run id must not reach the identity, or the profile is new every time.
    assert "run-a" not in name
    assert session == f"session:{name}"


@pytest.mark.asyncio
async def test_the_session_id_stays_unique(opener) -> None:
    """`session_id` names the session -- live-view and the logs address it --
    while `name` names the identity. Only the second is shared."""

    o = opener()
    await _loop()._open_warm_session(_run("run-a"), "www.walmart.com")
    assert o.session_ids == ["recipe-run-run-a"]


@pytest.mark.asyncio
async def test_two_runs_on_a_domain_reuse_the_same_small_pool(opener) -> None:
    """Across many runs the names must repeat, or nothing ever warms up.

    This is the regression: 807 distinct profile directories accumulated on one
    dev worker, none of them reused.
    """

    o = opener()
    worker = _loop()
    for i in range(40):
        await worker._open_warm_session(_run(f"run-{i}"), "www.walmart.com")

    assert len(set(o.asked)) <= loop_mod._IDENTITY_SLOTS
    assert set(o.asked) <= {f"recipe-{i}" for i in range(loop_mod._IDENTITY_SLOTS)}


@pytest.mark.asyncio
async def test_a_busy_slot_is_stepped_over(opener) -> None:
    """`Registry.acquire` refuses an identity that already holds an active
    lease, so concurrent runs on one domain must land on different slots."""

    o = opener({f"recipe-{i}" for i in range(loop_mod._IDENTITY_SLOTS) if i != 5})
    session = await _loop()._open_warm_session(_run("run-a"), "www.walmart.com")

    assert session == "session:recipe-5"
    # It tried the busy ones and moved on rather than giving up on the first.
    assert "recipe-5" in o.asked


@pytest.mark.asyncio
async def test_all_slots_busy_still_runs(opener) -> None:
    """A cold identity is likelier to be blocked; a run that never starts is
    certainly useless. `classify.py` can tell a block from a broken selector in
    the result, so executing and reporting beats refusing."""

    o = opener({f"recipe-{i}" for i in range(loop_mod._IDENTITY_SLOTS)})
    session = await _loop()._open_warm_session(_run("run-a"), "www.walmart.com")

    assert session == "session:recipe-run-run-a"
    assert len(o.asked) == loop_mod._IDENTITY_SLOTS + 1
