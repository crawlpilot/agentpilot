"""`assist.apply_resolutions` and friends -- what a person's answer does to a recipe.

Separate from `test_recipe_v2_assist.py` because that module imports `psycopg`
at the top for its Postgres-gated store tests, and nothing here needs a
database: these exercise the apply path against stubbed page access.
"""

from __future__ import annotations

from agentpilot.recipe.v2 import assist as assist_mod
from agentpilot.recipe.v2 import review as review_mod
from agentpilot.recipe.v2 import steps as steps_mod
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Locator,
    Recipe,
    Step,
    StepOutcome,
    TargetSpec,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.selector_agent import VerifiedLocator

_REVEAL = Step(
    op="click",
    target=Locator(kind="ax_role", role="button", name_contains="COMPOSITION, CARE & ORIGIN"),
)


def _recipe(picked: Locator) -> Recipe:
    return Recipe(
        recipe_id="r",
        tenant="dev",
        name="zara",
        version=1,
        target=TargetSpec(),
        fields={"origin": FieldSpec(name="origin", type=TypeSpec())},
        field_groups=[
            FieldGroup(
                group_id="group-0",
                field_names=["origin"],
                bindings={"origin": [Candidate(locator=picked)]},
                steps=[_REVEAL],
            )
        ],
    )


def _stub_page(monkeypatch) -> None:
    """Every step runs, every locator reads. The route is what is under test."""

    class _Reader:
        def invalidate(self) -> None:
            pass

        async def read(self, _locator):
            return "Made in Turkiye"

    async def _restore_page(*_a, **_k):
        return _Reader(), object()

    async def _run_steps(steps, _ctx, **_k):
        return [StepOutcome(index=i, op=s.op, status="ok") for i, s in enumerate(steps)], None

    async def _verify(locators, **_k):
        return (
            [
                VerifiedLocator(locator=loc, raw="Made in Turkiye", value="Made in Turkiye")
                for loc in locators
            ],
            None,
        )

    monkeypatch.setattr(review_mod, "restore_page", _restore_page)
    monkeypatch.setattr(steps_mod, "run_steps", _run_steps)
    monkeypatch.setattr(assist_mod, "verify_locators", _verify)


async def test_a_routed_pick_keeps_the_route_it_was_verified_behind(monkeypatch) -> None:
    """The binding is certified against the group's own route followed by the
    person's, so that is what has to be stored.

    `_with_steps` replaces rather than appends, so storing `resolution.steps`
    alone certified one page state and saved a different one. On a Zara product
    page that is exactly fatal: the group already clicks "COMPOSITION, CARE &
    ORIGIN", so a manual route for a field in it was saved without the click it
    was verified behind, and read nothing on every real run.
    """

    _stub_page(monkeypatch)
    picked = Locator(kind="css", selector=".origin-text")
    manual = Step(op="click", target=Locator(kind="css", selector="#show-origin"))

    out, problem = await assist_mod._apply_pick_after_steps(
        _recipe(picked),
        assist_mod.Resolution(
            field="origin", action="pick", locators=[picked], steps=[manual]
        ),
        url="https://www.zara.com/in/en/x.html",
        session=object(),
        registry=object(),
        driver=object(),
    )

    assert problem is None
    group = next(g for g in out.field_groups if "origin" in g.bindings)
    # Both halves, in order: the group's reveal, then what the person pointed at.
    assert [s.target.selector or s.target.name_contains for s in group.steps] == [
        "COMPOSITION, CARE & ORIGIN",
        "#show-origin",
    ]


async def test_the_teardown_half_stays_out_of_the_setup(monkeypatch) -> None:
    """A group runs its steps and THEN reads, so tidying cannot be setup."""

    _stub_page(monkeypatch)
    picked = Locator(kind="css", selector=".origin-text")

    out, problem = await assist_mod._apply_pick_after_steps(
        _recipe(picked),
        assist_mod.Resolution(
            field="origin",
            action="pick",
            locators=[picked],
            steps=[Step(op="click", target=Locator(kind="css", selector="#show-origin"))],
            teardown=[Step(op="click", target=Locator(kind="css", selector="#close"))],
        ),
        url="https://www.zara.com/in/en/x.html",
        session=object(),
        registry=object(),
        driver=object(),
    )

    assert problem is None
    group = next(g for g in out.field_groups if "origin" in g.bindings)
    assert [s.target.selector for s in group.teardown] == ["#close"]
    assert all(s.target.selector != "#close" for s in group.steps if s.target)
