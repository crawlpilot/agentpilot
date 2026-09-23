"""Candidate resolution and the assertion quality gate."""

from __future__ import annotations

from typing import Any

import pytest

from agentpilot.recipe.v2.models import Candidate, Locator, Predicate
from agentpilot.recipe.v2.resolve import (
    applicable_candidates,
    apply_assertions_to_status,
    evaluate_assertions,
    resolve_field,
)
from agentpilot.recipe.v2.schema import Assertion, FieldSpec, TypeSpec, parse_fields
from agentpilot.recipe.v2.transform import TransformContext, parse_transforms

NAME = parse_fields({"name": {"type": {"kind": "scalar", "value_type": "string"}}})["name"]
PRICE = parse_fields({
    "price": {"type": {"kind": "scalar", "value_type": "price"},
              "transform": [{"op": "cast", "to": "price"}]}
})["price"]


def page(values: dict[tuple, Any]):
    async def _evaluate(loc: Locator) -> Any:
        return values.get((loc.kind, loc.selector or loc.path))
    return _evaluate


def cand(kind="css", selector=None, path=None, **kw) -> Candidate:
    return Candidate(locator=Locator(kind=kind, selector=selector, path=path), **kw)


# --- ordering ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_lower_priority_number_is_tried_first() -> None:
    ordered = await applicable_candidates(
        [cand(selector="b", priority=50), cand(selector="a", priority=10)],
        variant_id=None,
    )
    assert [c.locator.selector for c in ordered] == ["a", "b"]


@pytest.mark.asyncio
async def test_equal_priority_keeps_document_order() -> None:
    """A recipe whose author never set `priority` must behave exactly as v1
    did: the ordering became explicit without becoming mandatory."""

    ordered = await applicable_candidates(
        [cand(selector="first"), cand(selector="second")], variant_id=None
    )
    assert [c.locator.selector for c in ordered] == ["first", "second"]


@pytest.mark.asyncio
async def test_candidates_scoped_to_another_variant_are_dropped() -> None:
    ordered = await applicable_candidates(
        [cand(selector="a", variant_id="legacy"), cand(selector="b", variant_id="modern"),
         cand(selector="c")],
        variant_id="modern",
    )
    assert [c.locator.selector for c in ordered] == ["b", "c"]


@pytest.mark.asyncio
async def test_unscoped_candidates_survive_when_no_variant_matched() -> None:
    """No variant matching is degraded, not failed -- the variant-agnostic
    candidates must still run."""

    ordered = await applicable_candidates(
        [cand(selector="a", variant_id="legacy"), cand(selector="c")], variant_id=None
    )
    assert [c.locator.selector for c in ordered] == ["c"]


@pytest.mark.asyncio
async def test_guarded_candidate_is_skipped_when_its_predicate_fails() -> None:
    async def holds(p: Predicate) -> bool:
        return p.selector == "#yes"

    ordered = await applicable_candidates(
        [
            cand(selector="a", when=[Predicate(kind="visible", selector="#no")]),
            cand(selector="b", when=[Predicate(kind="visible", selector="#yes")]),
        ],
        variant_id=None,
        predicate_holds=holds,
    )
    assert [c.locator.selector for c in ordered] == ["b"]


# --- resolution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_non_empty_wins_and_is_marked_resolved() -> None:
    res = await resolve_field(
        NAME, [cand(selector="a"), cand(selector="b")],
        evaluate=page({("css", "a"): "Dove"}), ctx=TransformContext(),
    )
    assert (res.value, res.status, res.candidate_index) == ("Dove", "resolved", 0)


@pytest.mark.asyncio
async def test_falling_through_to_a_later_candidate_is_marked_fallback() -> None:
    """The drift signal: a field resolving from candidate 1 instead of 0 is
    breaking, days before it breaks."""

    res = await resolve_field(
        NAME, [cand(selector="a"), cand(selector="b")],
        evaluate=page({("css", "b"): "Dove"}), ctx=TransformContext(),
    )
    assert (res.status, res.candidate_index, res.source) == ("fallback", 1, "css")


@pytest.mark.asyncio
async def test_a_candidate_whose_transform_empties_the_value_falls_through() -> None:
    """Transforming before deciding is deliberate: a candidate that resolves to
    something which casts to None has not actually produced a value."""

    res = await resolve_field(
        PRICE, [cand(selector="a"), cand(selector="b")],
        evaluate=page({("css", "a"): "no digits", ("css", "b"): "$6.97"}),
        ctx=TransformContext(),
    )
    assert (res.value, res.status, res.candidate_index) == (6.97, "fallback", 1)


@pytest.mark.asyncio
async def test_per_candidate_transform_overrides_the_fields_pipeline() -> None:
    structured = Candidate(
        locator=Locator(kind="json_ld", path="p"),
        transform=parse_transforms([{"op": "cast", "to": "price"}]),
    )
    res = await resolve_field(
        PRICE, [structured], evaluate=page({("json_ld", "p"): "9550"}),
        ctx=TransformContext(),
    )
    assert res.value == 9550.0


@pytest.mark.asyncio
async def test_empty_transform_list_means_no_cleanup_not_the_fields_pipeline() -> None:
    raw_through = Candidate(locator=Locator(kind="css", selector="a"), transform=[])
    res = await resolve_field(
        PRICE, [raw_through], evaluate=page({("css", "a"): "$6.97"}),
        ctx=TransformContext(),
    )
    assert res.value == "$6.97"


@pytest.mark.asyncio
async def test_required_field_with_nothing_resolving_fails() -> None:
    required = FieldSpec(name="x", type=TypeSpec(), required=True)
    res = await resolve_field(required, [cand(selector="a")], evaluate=page({}),
                              ctx=TransformContext())
    assert res.status == "failed"


@pytest.mark.asyncio
async def test_optional_field_with_nothing_resolving_is_empty_not_failed() -> None:
    res = await resolve_field(NAME, [cand(selector="a")], evaluate=page({}),
                              ctx=TransformContext())
    assert res.status == "empty"


@pytest.mark.asyncio
async def test_an_exploding_locator_does_not_stop_the_later_candidates() -> None:
    async def evaluate(loc: Locator) -> Any:
        if loc.selector == "bad":
            raise ValueError("invalid xpath")
        return "Dove"

    res = await resolve_field(
        NAME, [cand(selector="bad"), cand(selector="good")],
        evaluate=evaluate, ctx=TransformContext(),
    )
    assert (res.value, res.status) == ("Dove", "fallback")


@pytest.mark.asyncio
async def test_no_applicable_candidate_reports_why() -> None:
    res = await resolve_field(
        NAME, [cand(selector="a", variant_id="other")], evaluate=page({}),
        ctx=TransformContext(), variant_id="mine",
    )
    assert res.status == "empty" and "no candidate applies" in res.reason


# --- assertions -------------------------------------------------------------


def test_range_and_length_and_in_set() -> None:
    assert evaluate_assertions(5, [Assertion(kind="range", min=1, max=10)])[0].passed
    assert not evaluate_assertions(0, [Assertion(kind="range", min=1)])[0].passed
    assert evaluate_assertions("abc", [Assertion(kind="length", min=2, max=5)])[0].passed
    assert evaluate_assertions(
        "USD", [Assertion(kind="in_set", values=["USD", "INR"])]
    )[0].passed


def test_range_on_a_non_number_fails_rather_than_raising() -> None:
    result = evaluate_assertions("n/a", [Assertion(kind="range", min=1)])[0]
    assert not result.passed and "not a number" in result.detail


def test_matches_with_an_invalid_regex_fails_cleanly() -> None:
    result = evaluate_assertions("x", [Assertion(kind="matches", regex="(unclosed")])[0]
    assert not result.passed and "invalid regex" in result.detail


def test_not_empty_catches_the_geo_gated_price() -> None:
    """Amazon's price node resolved to '' on a geo-gated load while the page
    rendered fine."""

    assert not evaluate_assertions("", [Assertion(kind="not_empty")])[0].passed
    assert not evaluate_assertions(None, [Assertion(kind="not_empty")])[0].passed


def test_cross_source_agreement_catches_the_ad_price() -> None:
    """The Walmart trap: the page's own JSON carries a sponsored competitor's
    $5.22 alongside the real $6.97. Reading both and comparing is nearly free
    and is the specific defence."""

    a = Assertion(kind="cross_source_agrees", tolerance=0.01)
    assert evaluate_assertions(6.97, [a], cross_source_value=6.97)[0].passed
    disagree = evaluate_assertions(6.97, [a], cross_source_value=5.22)[0]
    assert not disagree.passed and "disagree" in disagree.detail


def test_cross_source_with_no_second_source_is_recorded_not_silently_passed() -> None:
    result = evaluate_assertions(1, [Assertion(kind="cross_source_agrees")])[0]
    assert result.passed and "no second source" in result.detail


def test_cross_source_compares_numerically_across_string_and_float() -> None:
    a = Assertion(kind="cross_source_agrees", tolerance=0.01)
    assert evaluate_assertions("6.97", [a], cross_source_value=6.97)[0].passed


def test_a_failed_assertion_downgrades_to_suspect_and_keeps_the_value() -> None:
    """Discarding data on a heuristic is worse than flagging it -- and
    `suspect` is what the drift metrics trend on."""

    failing = [Assertion(kind="range", min=100)]
    results = evaluate_assertions(1, failing)
    assert apply_assertions_to_status("resolved", results) == "suspect"
    assert apply_assertions_to_status("fallback", results) == "suspect"


def test_assertions_never_upgrade_a_failed_field() -> None:
    passing = evaluate_assertions(5, [Assertion(kind="range", min=1)])
    assert apply_assertions_to_status("failed", passing) == "failed"
    assert apply_assertions_to_status("empty", passing) == "empty"


def test_unknown_assertion_kind_is_skipped_not_fatal() -> None:
    result = evaluate_assertions(1, [Assertion(kind="from_the_future")])[0]
    assert result.passed and "skipped" in result.detail


# --- which selector matched, and what the others did ------------------------


@pytest.mark.asyncio
async def test_the_winning_locator_travels_with_the_value() -> None:
    """`source` is only the locator's KIND, and on a field with four css
    candidates the kind is the one thing that does not distinguish them. Without
    the locator itself, "this value came from css" cannot be acted on."""

    res = await resolve_field(
        NAME,
        [cand(selector="#gone"), cand(selector="[itemprop=name]")],
        evaluate=page({("css", "[itemprop=name]"): "Bodycology"}),
        ctx=TransformContext(),
    )

    assert res.value == "Bodycology"
    assert res.locator is not None
    assert res.locator.selector == "[itemprop=name]"
    # The position only means something next to the length of the chain.
    assert res.candidate_index == 1
    assert res.considered == 2
    assert res.status == "fallback"


@pytest.mark.asyncio
async def test_every_candidate_ahead_of_the_winner_is_accounted_for() -> None:
    """"Fell through to candidate 2" is half a diagnosis. The other half is
    whether the ones ahead of it stopped matching or merely stopped producing a
    usable value -- different breakages, different fixes."""

    res = await resolve_field(
        PRICE,
        [
            cand(selector="#gone"),
            cand(selector=".legend"),
            cand(selector=".price"),
        ],
        evaluate=page({
            # Matches nothing at all any more.
            (".legend", None): None,
            ("css", ".legend"): "Contact us for pricing",
            ("css", ".price"): "$4.97",
        }),
        ctx=TransformContext(),
    )

    assert res.value == 4.97
    assert [(a.index, a.outcome) for a in res.attempts] == [
        (0, "empty"),
        (1, "cleaned_to_nothing"),
        (2, "won"),
    ]
    # The selector that is fine but whose value no longer survives the cast is
    # the most misleading of the failures, so it says what it read.
    assert res.attempts[1].detail == "Contact us for pricing"
    assert res.attempts[2].locator.selector == ".price"


@pytest.mark.asyncio
async def test_a_locator_that_throws_is_recorded_as_having_thrown() -> None:
    async def explodes(loc: Locator) -> Any:
        if loc.selector == "#bad":
            raise ValueError("'#bad' is not a valid selector")
        return "Dove"

    res = await resolve_field(
        NAME,
        [cand(selector="#bad"), cand(selector=".ok")],
        evaluate=explodes,
        ctx=TransformContext(),
    )

    assert res.value == "Dove"
    assert res.attempts[0].outcome == "raised"
    assert "not a valid selector" in (res.attempts[0].detail or "")


@pytest.mark.asyncio
async def test_a_field_that_resolved_nothing_still_says_what_it_tried() -> None:
    """The case most worth having the attempts for: an empty field with no
    winning locator to point at."""

    res = await resolve_field(
        NAME,
        [cand(selector="#a"), cand(selector="#b")],
        evaluate=page({}),
        ctx=TransformContext(),
    )

    assert res.status == "empty"
    assert res.locator is None
    assert res.considered == 2
    assert [a.outcome for a in res.attempts] == ["empty", "empty"]


@pytest.mark.asyncio
async def test_an_attempt_serializes_to_something_a_person_can_read() -> None:
    res = await resolve_field(
        NAME,
        [cand(selector=".n")],
        evaluate=page({("css", ".n"): "Dove"}),
        ctx=TransformContext(),
    )
    # `Locator.to_dict` omits defaults, so a provenance payload carries only
    # what actually distinguishes one selector from another.
    assert res.attempts[0].to_dict() == {
        "index": 0,
        "outcome": "won",
        "locator": {"kind": "css", "selector": ".n"},
    }
    # `detail` is absent rather than null when there is nothing to say.
    assert "detail" not in res.attempts[0].to_dict()
