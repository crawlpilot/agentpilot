"""`agentpilot.recipe.v2.review` -- run the draft, judge it, repair, decide.

No browser and no LLM: `replay_recipe` and `judge_collection` are stubbed, so
what is under test is the loop's termination and the gate's direction, which is
the part that can quietly cost either a person's time or a wrong recipe.
"""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2 import review as review_mod
from agentpilot.recipe.v2.judge import DataVerdict, FieldVerdict
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Locator,
    Recipe,
    RecipeRunResult,
    TargetSpec,
)
from agentpilot.recipe.v2.review import (
    ReviewResult,
    SampleRun,
    merge_field_values,
    verify_and_judge,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec


def _recipe() -> Recipe:
    return Recipe(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(),
        fields={
            "name": FieldSpec(name="name", type=TypeSpec(kind="scalar")),
            "price": FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="price")),
        },
        field_groups=[
            FieldGroup(
                group_id="g0",
                field_names=["name", "price"],
                bindings={
                    "name": [Candidate(locator=Locator(kind="css", selector=".crumb"))],
                    "price": [Candidate(locator=Locator(kind="css", selector=".p"))],
                },
            )
        ],
    )


@pytest.fixture
def stubs(monkeypatch):
    """Replay returns fixed data; the judge returns a scripted verdict list."""

    state = {
        "data": {"name": "Home / Tops", "price": 2290},
        "verdicts": [],
        "repairs": [],
        "replays": 0,
    }

    async def fake_replay(recipe, run_input, **kwargs):
        state["replays"] += 1
        return RecipeRunResult(
            outcome="ok",
            data=dict(state["data"]),
            field_status={k: "resolved" for k in state["data"]},
        )

    async def fake_judge(fields, **kwargs):
        if state["verdicts"]:
            return state["verdicts"].pop(0)
        return DataVerdict(passed=True)

    async def fake_repair(recipe, rejected, **kwargs):
        state["repairs"].append(dict(rejected))
        return recipe, state.get("repair_returns", set())

    class _Reader:
        def invalidate(self): ...
        async def snapshot(self): return None

    monkeypatch.setattr(review_mod, "replay_recipe", fake_replay)
    monkeypatch.setattr(review_mod, "judge_collection", fake_judge)
    monkeypatch.setattr(review_mod, "repair_fields", fake_repair)
    monkeypatch.setattr(review_mod, "PageReader", lambda **k: _Reader())
    return state


async def _run(state, **kwargs):
    return await verify_and_judge(
        _recipe(), sample_urls=["https://x.test/p/1"],
        session=None, registry=None, driver=None, llm_config=None, **kwargs,
    )


# --- the happy path ---------------------------------------------------------


async def test_a_passing_draft_is_ready_for_review_without_repair(stubs) -> None:
    _recipe_out, result = await _run(stubs)

    assert result.ready_for_review is True
    assert result.repairs == 0
    assert result.unrepaired == {}
    assert result.runs[0].outcome == "ok"


# --- the repair loop --------------------------------------------------------


async def test_a_rejection_is_fed_back_and_the_repair_can_fix_it(stubs) -> None:
    stubs["verdicts"] = [
        DataVerdict(passed=False, verdicts={
            "name": FieldVerdict("name", False, "this is the breadcrumb trail"),
        }),
        DataVerdict(passed=True),
    ]
    stubs["repair_returns"] = {"name"}

    _recipe_out, result = await _run(stubs)

    assert result.repairs == 1
    # The judge's own words reach the repair, because they ARE the instruction.
    assert stubs["repairs"][0] == {"name": "this is the breadcrumb trail"}
    assert result.unrepaired == {}
    assert result.ready_for_review is True


async def test_the_repair_budget_is_bounded(stubs) -> None:
    """Each round costs a page load, the group's step sequence and a proposal
    call."""

    stubs["verdicts"] = [
        DataVerdict(passed=False, verdicts={"name": FieldVerdict("name", False, "no")})
        for _ in range(6)
    ]
    stubs["repair_returns"] = {"name"}

    _recipe_out, result = await _run(stubs, max_repairs=2)

    assert result.repairs == 2
    assert result.unrepaired == {"name": "no"}
    assert result.ready_for_review is False


async def test_a_repair_that_changes_nothing_stops_the_loop(stubs) -> None:
    """A model that cannot find a better locator will happily propose the same
    one forever, and each round costs a full page load to learn that again."""

    stubs["verdicts"] = [
        DataVerdict(passed=False, verdicts={"name": FieldVerdict("name", False, "no")})
        for _ in range(6)
    ]
    stubs["repair_returns"] = set()

    _recipe_out, result = await _run(stubs, max_repairs=5)

    assert result.repairs == 1
    assert result.unrepaired == {"name": "no"}


# --- the gate ---------------------------------------------------------------


async def test_an_errored_judge_still_reaches_a_human(stubs) -> None:
    """It could not ask the question, and a human is the gate it was standing
    in front of. Parking the run instead would wait on a person for something
    the judge never managed to ask."""

    stubs["verdicts"] = [DataVerdict(passed=True, errored=True)]

    _recipe_out, result = await _run(stubs)

    assert result.ready_for_review is True
    assert result.verdict.errored is True


def test_a_draft_that_was_never_judged_is_not_ready() -> None:
    assert ReviewResult().ready_for_review is False


# --- merging across sample runs ---------------------------------------------


def test_a_field_that_resolved_on_any_page_is_the_one_judged() -> None:
    """A field empty on page one and present on page two is a field that
    works. Judging the empty reading would report a completeness problem as a
    correctness problem, and the two need opposite fixes."""

    runs = [
        SampleRun(url="a", outcome="ok", data={"name": "", "price": 10}),
        SampleRun(url="b", outcome="ok", data={"name": "Ribbed top", "price": 10}),
    ]
    assert merge_field_values(runs) == {"name": "Ribbed top", "price": 10}


def test_empty_readings_alone_merge_to_nothing() -> None:
    runs = [SampleRun(url="a", outcome="ok", data={"name": None, "tags": []})]
    assert merge_field_values(runs) == {}


async def test_a_replay_that_raises_is_recorded_not_fatal(stubs, monkeypatch) -> None:
    """One bad sample URL must not lose the evidence from the others."""

    async def boom(recipe, run_input, **kwargs):
        if run_input.url.endswith("/2"):
            raise RuntimeError("navigation timeout")
        return RecipeRunResult(outcome="ok", data={"name": "Ribbed top"})

    monkeypatch.setattr(review_mod, "replay_recipe", boom)

    _recipe_out, result = await verify_and_judge(
        _recipe(), sample_urls=["https://x.test/p/1", "https://x.test/p/2"],
        session=None, registry=None, driver=None, llm_config=None, sample_limit=2,
    )

    assert [r.outcome for r in result.runs] == ["ok", "failed"]
    assert "navigation timeout" in (result.runs[1].error or "")
    assert result.ready_for_review is True


async def test_sample_runs_are_capped(stubs) -> None:
    """Each is a full page load plus the whole step sequence; the third is
    mostly confirmation."""

    await verify_and_judge(
        _recipe(), sample_urls=[f"https://x.test/p/{i}" for i in range(9)],
        session=None, registry=None, driver=None, llm_config=None, sample_limit=2,
    )
    assert stubs["replays"] == 2
