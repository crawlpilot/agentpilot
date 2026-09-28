"""What the sample runs proved, counted from what they actually recorded.

`verified_on` meant nothing until now -- hardcoded `1` at every site that built
a candidate, while the frontend lint warned on `0` claiming the field was
"written by build-time multi-page induction". These tests pin the metric, and
in particular pin the definition that is *not* used, because it is the
attractive one: crediting every candidate a page resolved "at or before" makes
the never-executed last resort the best-verified selector in the recipe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentpilot.recipe.v2.merge import apply_verified_counts, verified_counts
from agentpilot.recipe.v2.models import Candidate, FieldGroup, Locator, Recipe, TargetSpec
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

JSON_LD = Locator(kind="json_ld", path="[0].name")
CSS = Locator(kind="css", selector="h1")
XPATH = Locator(kind="xpath", selector="//h1")


@dataclass
class Run:
    """Just the shape `merge.RunLike` asks for."""

    outcome: str = "ok"
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)


def won(locator: Locator, *, at: int, of: int = 3) -> dict[str, Any]:
    """A scalar field's provenance for a run where `locator` produced the value."""

    return {
        "candidate": at,
        "candidates": of,
        "source": locator.kind,
        "locator": locator.to_dict(),
        "attempts": [{"index": at, "outcome": "won", "locator": locator.to_dict()}],
    }


def chain(*locators: Locator) -> list[Candidate]:
    return [Candidate(locator=loc, priority=10 * (i + 1)) for i, loc in enumerate(locators)]


def recipe_with(bindings: dict[str, list[Candidate]], repeat=None) -> Recipe:
    return Recipe(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(match=[]),
        fields={"name": FieldSpec(name="name", type=TypeSpec(value_type="string"))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=list(bindings), bindings=bindings, repeat=repeat,
        )],
    )


def counts_for(r: Recipe, runs: list[Run]) -> list[int]:
    applied = apply_verified_counts(r, verified_counts(runs))
    binding = next(iter(applied.field_groups[0].bindings))
    return [c.verified_on for c in applied.field_groups[0].bindings[binding]]


# --- the metric -------------------------------------------------------------


def test_a_candidate_is_credited_for_the_pages_it_won_on() -> None:
    r = recipe_with({"name": chain(JSON_LD, CSS, XPATH)})
    runs = [Run(provenance={"name": won(JSON_LD, at=0)}) for _ in range(3)]
    assert counts_for(r, runs) == [3, 0, 0]


def test_a_never_reached_fallback_is_not_credited() -> None:
    """The definition this module exists to reject.

    Crediting every candidate the field resolved "at or before" would score this
    chain `3, 3, 3` -- the positional xpath nothing has ever executed reading as
    well-verified as the JSON-LD path that did all the work. That inflates
    exactly the brittle tail `selector_quality` exists to demote.
    """

    r = recipe_with({"name": chain(JSON_LD, CSS, XPATH)})
    runs = [Run(provenance={"name": won(JSON_LD, at=0)}) for _ in range(3)]
    assert counts_for(r, runs)[2] == 0, "it was never evaluated, so nothing is proven"


def test_credit_follows_the_winner_down_the_chain() -> None:
    """A drifting primary: two pages fell through to the CSS candidate."""

    r = recipe_with({"name": chain(JSON_LD, CSS, XPATH)})
    runs = [
        Run(provenance={"name": won(JSON_LD, at=0)}),
        Run(provenance={"name": won(CSS, at=1)}),
        Run(provenance={"name": won(CSS, at=1)}),
    ]
    assert counts_for(r, runs) == [1, 2, 0]


def test_a_field_that_never_resolved_credits_nobody() -> None:
    r = recipe_with({"name": chain(JSON_LD, CSS)})
    runs = [Run(provenance={"name": {
        "candidate": None, "candidates": 2,
        "attempts": [
            {"index": 0, "outcome": "empty", "locator": JSON_LD.to_dict()},
            {"index": 1, "outcome": "empty", "locator": CSS.to_dict()},
        ],
    }})]
    assert counts_for(r, runs) == [0, 0]


def test_a_blocked_run_is_not_evidence_about_a_selector() -> None:
    """The same mistake `classify.py` exists to prevent, one rung down. A wall in
    front of the page says nothing about whether a selector still matches."""

    r = recipe_with({"name": chain(JSON_LD, CSS)})
    runs = [
        Run(provenance={"name": won(JSON_LD, at=0)}),
        Run(outcome="blocked", provenance={"name": won(CSS, at=1)}),
    ]
    assert counts_for(r, runs) == [1, 0]


def test_counting_replaces_rather_than_accumulates() -> None:
    """A recipe re-reviewed after a redesign must not keep credit its selectors
    earned against the old markup."""

    r = recipe_with({"name": [Candidate(locator=JSON_LD, verified_on=9)]})
    assert counts_for(r, [Run(provenance={"name": won(JSON_LD, at=0)})]) == [1]


def test_a_locator_whose_defaults_were_omitted_still_matches() -> None:
    """Provenance carries `to_dict()`, which omits defaults. Keying the raw dict
    instead of round-tripping it through `from_dict` would miss on nearly every
    locator, and silently score the whole recipe zero."""

    css = Locator(kind="css", selector=".price", attribute="text")
    assert "attribute" not in css.to_dict(), "the default is omitted -- that is the trap"
    r = recipe_with({"name": [Candidate(locator=css)]})
    assert counts_for(r, [Run(provenance={"name": won(css, at=0)})]) == [1]


# --- tables -----------------------------------------------------------------


def test_a_table_column_is_credited_by_position() -> None:
    """A column's provenance carries only its FIRST candidate's locator, so the
    winner is identified by `won_at` -- which is why `RepeatDiagnosis` records
    it."""

    from agentpilot.recipe.v2.models import RepeatSpec

    r = recipe_with(
        {"value": chain(CSS, XPATH)},
        repeat=RepeatSpec(kind="dom_rows", row_field="specs", max_iterations=100,
                          rows_locator=Locator(kind="css", selector="tr")),
    )
    runs = [
        Run(provenance={"specs": {
            "rows": 5, "rows_matched": 5, "repeat_kind": "dom_rows",
            "columns": {"value": {"locator": CSS.to_dict(), "filled": 5, "of": 5, "won_at": 1}},
        }}),
        Run(provenance={"specs": {
            "rows": 4, "rows_matched": 4, "repeat_kind": "dom_rows",
            "columns": {"value": {"locator": CSS.to_dict(), "filled": 4, "of": 4, "won_at": 1}},
        }}),
    ]
    assert counts_for(r, runs) == [0, 2], "the fallback did the work on both pages"


def test_a_column_that_filled_nothing_credits_nobody() -> None:
    from agentpilot.recipe.v2.models import RepeatSpec

    r = recipe_with(
        {"value": chain(CSS)},
        repeat=RepeatSpec(kind="dom_rows", row_field="specs", max_iterations=100,
                          rows_locator=Locator(kind="css", selector="tr")),
    )
    runs = [Run(provenance={"specs": {
        "rows": 0, "rows_matched": 22, "repeat_kind": "dom_rows",
        "columns": {"value": {"locator": CSS.to_dict(), "filled": 0, "of": 22}},
    }})]
    assert counts_for(r, runs) == [0]


# --- shape ------------------------------------------------------------------


def test_applying_counts_leaves_everything_else_alone() -> None:
    r = recipe_with({"name": chain(JSON_LD, CSS)})
    before = r.field_groups[0]
    applied = apply_verified_counts(r, verified_counts([Run(provenance={
        "name": won(JSON_LD, at=0)
    })]))
    after = applied.field_groups[0]
    assert after.group_id == before.group_id
    assert [c.priority for c in after.bindings["name"]] == [
        c.priority for c in before.bindings["name"]
    ]
    assert [c.locator for c in after.bindings["name"]] == [
        c.locator for c in before.bindings["name"]
    ]
    # And the original is untouched -- frozen dataclasses, a new Recipe out.
    assert before.bindings["name"][0].verified_on == 0


def test_no_runs_is_not_an_error() -> None:
    r = recipe_with({"name": chain(JSON_LD)})
    assert counts_for(r, []) == [0]
