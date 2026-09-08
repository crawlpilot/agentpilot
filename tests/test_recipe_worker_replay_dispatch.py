"""Which engine a replay run is dispatched to.

This is a one-line decision that silently produced empty results for every
recipe anyone has actually built, so it is worth a test of its own.

`_to_recipe_model` reads `global_setup`/`field_groups` -- the v1 columns.
`_process_onboard` writes those as `[]` on purpose (a v2 group has
`bindings`/`steps`, and putting one in a v1-named column is the shape confusion
that crashed the recipe detail page). So sending a v2 recipe to the v1 engine
replays a recipe with no field groups at all: it completes, reports success,
and returns `{}`.

A run that collects nothing and calls itself healthy is the worst of both
outcomes -- the caller gets nulls and the recipe gets marked fine.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentpilot.jobs.recipe_store import ClaimedRecipeRun, RecipeOut
from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop

V2_DOCUMENT: dict[str, Any] = {
    "name": "n",
    "version": 1,
    "fields": {"title": {"type": {"kind": "scalar", "value_type": "string"}}},
    "target": {"match": [{"kind": "glob", "pattern": "https://x.test/*"}]},
    "field_groups": [
        {
            "group_id": "g0",
            "field_names": ["title"],
            "bindings": {"title": [{"locator": {"kind": "css", "selector": "h1"}}]},
        }
    ],
}


def _run(*, document: dict[str, Any] | None, job_id: str | None = None, url: str | None = None):
    recipe = RecipeOut(
        recipe_id="r1", tenant="acme", name="n", url_pattern="https://x.test/p/1",
        field_schema={}, version=1,
        # Empty, exactly as an onboarding build leaves them.
        global_setup=[], field_groups=[],
        health_status="healthy", last_verified_at=None, last_run_at=None,
        schedule_interval_seconds=None, created_at=None, updated_at=None,  # type: ignore[arg-type]
        document=document,
    )
    return ClaimedRecipeRun(
        run_id="run-1", recipe_id="r1", tenant="acme", kind="replay",
        params=None, lock="lk", recipe=recipe, job_id=job_id, url=url,
    )


class _Loop(RecipeWorkerLoop):
    """Records which branch was taken instead of driving a browser."""

    def __init__(self) -> None:
        super().__init__(
            store=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            driver=None,  # type: ignore[arg-type]
            profiles_root=None,  # type: ignore[arg-type]
            proxy_pinner=None,
        )
        self.took: list[str] = []

    async def _process_job_run(self, run, session):  # type: ignore[override]
        self.took.append("job")

    async def _process_replay_v2(self, run, session, document, url):  # type: ignore[override]
        self.took.append("v2")
        self.url = url

    async def _complete_from_result(self, run, result):  # type: ignore[override]
        self.took.append("completed")


@pytest.fixture
def loop(monkeypatch):
    made = _Loop()

    async def fake_v1(recipe, **kwargs):
        made.took.append("v1")
        from agentpilot.recipe.models import RecipeRunResult

        return RecipeRunResult(success=True, data={}, field_failures={})

    monkeypatch.setattr("agentpilot.jobs.recipe_worker_loop.replay_recipe", fake_v1)

    class _Store:
        async def mark_replay_result(self, *a, **k): ...

    made._store = _Store()  # type: ignore[assignment]
    return made


async def test_a_v2_recipe_replays_through_the_v2_engine(loop) -> None:
    """The regression. The v1 engine reads columns that are empty for every
    studio- and agent-built recipe, so it returned `{}` for all of them."""

    await loop._process_replay(_run(document=V2_DOCUMENT), None, "https://x.test/p/1")
    assert loop.took == ["v2"]


async def test_the_submitted_url_is_what_v2_runs_against(loop) -> None:
    await loop._process_replay(_run(document=V2_DOCUMENT), None, "https://x.test/p/9")
    assert loop.url == "https://x.test/p/9"


async def test_a_genuinely_v1_recipe_still_uses_the_v1_engine(loop) -> None:
    """Nothing about this fix may change what happens to a recipe that has no
    v2 document -- that path is the only thing v1 recipes have."""

    await loop._process_replay(_run(document=None), None, "https://x.test/p/1")
    assert loop.took == ["v1", "completed"]


async def test_a_job_run_still_goes_to_the_job_path(loop) -> None:
    """A submitted run is dispatched by carrying its own URL, and must keep
    that route -- it is the one whose failures must NOT move recipe health."""

    run = _run(document=V2_DOCUMENT, job_id="job-1", url="https://x.test/p/2")
    await loop._process_replay(run, None, "https://x.test/p/2")
    assert loop.took == ["job"]
