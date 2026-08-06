"""Pure unit tests for `agentpilot.recipe.models` -- `to_dict()`/`from_dict()`
round-trips for every JSONB-serialized shape (these are exactly what
`recipe_store.py` persists and re-hydrates)."""

from __future__ import annotations

from agentpilot.recipe.models import (
    FieldGroup,
    FieldLocator,
    Locator,
    Recipe,
    RepeatSpec,
    RevealStep,
)


def test_locator_round_trips_through_dict() -> None:
    locator = Locator(source="ax_role", role="button", name_contains="Buy", name_in=["S", "M"])
    assert Locator.from_dict(locator.to_dict()) == locator


def test_reveal_step_round_trips_with_a_locator() -> None:
    step = RevealStep(action="fill", locator=Locator(source="css", selector="#q"), value="hello")
    assert RevealStep.from_dict(step.to_dict()) == step


def test_reveal_step_round_trips_with_no_locator() -> None:
    step = RevealStep(action="wait", locator=None, value="500")
    assert RevealStep.from_dict(step.to_dict()) == step


def test_repeat_spec_round_trips() -> None:
    repeat = RepeatSpec(
        option_locator=Locator(source="ax_role", role="button", name_in=["S", "M", "L"]),
        max_iterations=20,
        array_field="variants",
    )
    assert RepeatSpec.from_dict(repeat.to_dict()) == repeat


def test_field_group_round_trips_with_repeat() -> None:
    group = FieldGroup(
        group_id="g1",
        field_names=["size", "price"],
        reveal_steps=[RevealStep(action="click", locator=Locator(source="ax_role", role="tab"))],
        field_locators={"price": FieldLocator(source="json_ld", path="offers.price")},
        repeat=RepeatSpec(
            option_locator=Locator(source="ax_role", role="button", name_in=["S", "M"]),
            max_iterations=5,
            array_field="variants",
        ),
    )
    restored = FieldGroup.from_dict(group.to_dict())
    assert restored == group


def test_field_group_round_trips_without_repeat() -> None:
    group = FieldGroup(
        group_id="g0",
        field_names=["title"],
        reveal_steps=[],
        field_locators={"title": FieldLocator(source="css", selector="h1", attribute="text")},
    )
    assert FieldGroup.from_dict(group.to_dict()) == group


def test_recipe_groups_from_dict_reconstructs_global_setup_and_groups() -> None:
    recipe = Recipe(
        recipe_id="r1",
        tenant="t1",
        name="test",
        url_pattern="https://example.test/p",
        field_schema={"title": {"type": "scalar", "description": "d"}},
        version=1,
        global_setup=[
            RevealStep(action="click", locator=Locator(source="css", selector="#accept"))
        ],
        field_groups=[
            FieldGroup(
                group_id="g0",
                field_names=["title"],
                reveal_steps=[],
                field_locators={"title": FieldLocator(source="css", selector="h1")},
            )
        ],
    )
    global_setup, field_groups = Recipe.groups_from_dict(recipe.to_dict())
    assert global_setup == recipe.global_setup
    assert field_groups == recipe.field_groups
