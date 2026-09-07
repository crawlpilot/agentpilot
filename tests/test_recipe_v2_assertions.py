"""`agentpilot.recipe.v2.assertions` -- what would make a collected value wrong.

The baseline half is pure. The proposed half is parsed from a model reply, and
what is tested there is mostly what gets *rejected*: an assertion that cannot be
evaluated fails every run rather than being skipped, so a malformed proposal is
strictly worse than none.
"""

from __future__ import annotations

from agentpilot.recipe.v2.assertions import (
    baseline_assertions,
    parse_assertions,
    with_assertions,
)
from agentpilot.recipe.v2.models import Candidate, Locator
from agentpilot.recipe.v2.resolve import evaluate_assertions
from agentpilot.recipe.v2.schema import Assertion, FieldSpec, TypeSpec

PRICE = FieldSpec(
    name="price", required=True, type=TypeSpec(kind="scalar", value_type="price")
)
TITLE = FieldSpec(name="title", type=TypeSpec(kind="scalar", value_type="string"))
FIELDS = {"price": PRICE, "title": TITLE}


def _chain(*kinds: str) -> list[Candidate]:
    return [
        Candidate(
            locator=Locator(kind=k, selector="x" if k in ("css", "xpath") else None,
                            path="p" if k in ("json_ld", "hydration", "meta") else None)
        )
        for k in kinds
    ]


# --- baseline ---------------------------------------------------------------


def test_a_required_field_must_not_be_empty() -> None:
    got = baseline_assertions(FIELDS)
    assert [a.kind for a in got["price"]] == ["not_empty", "range"]
    # A field nobody called required gets nothing it did not earn.
    assert "title" not in got


def test_a_price_may_not_be_negative() -> None:
    """A negative price is a parse that went wrong -- a currency symbol read as
    a minus sign, or a discount picked up instead of the price."""

    got = baseline_assertions({"price": PRICE})
    ranges = [a for a in got["price"] if a.kind == "range"]
    assert ranges and ranges[0].min == 0

    results = evaluate_assertions(-5, ranges)
    assert results[0].passed is False


def test_two_kinds_of_locator_earn_a_cross_source_check() -> None:
    """The strongest automatic defence there is against the failure that
    silently poisons a dataset: a locator resolving to a well-typed value from
    the wrong product. `replay.py` calls the second read "nearly free"."""

    got = baseline_assertions(FIELDS, {"price": _chain("hydration", "css")})
    assert "cross_source_agrees" in [a.kind for a in got["price"]]


def test_two_candidates_of_the_same_kind_do_not() -> None:
    """Two CSS selectors onto the same rendered element are not a second
    opinion -- they would agree even when both are wrong."""

    got = baseline_assertions(FIELDS, {"price": _chain("css", "css")})
    assert "cross_source_agrees" not in [a.kind for a in got["price"]]


def test_a_table_field_gets_no_cross_source_check() -> None:
    """Its bindings are keyed by column, so the field itself has no chain --
    and an assertion on a table is about its rows, not a cell."""

    table = FieldSpec(
        name="variants", type=TypeSpec(kind="table", columns={"size": TypeSpec()})
    )
    got = baseline_assertions({"variants": table}, {"size": _chain("hydration", "css")})
    assert got == {}


def test_merging_keeps_what_the_author_already_declared() -> None:
    """They said what wrong looks like for their data; a proposal is a
    suggestion."""

    theirs = FieldSpec(
        name="price",
        required=True,
        type=TypeSpec(kind="scalar", value_type="price"),
        assertions=[Assertion(kind="range", min=1, max=99)],
    )
    merged = with_assertions({"price": theirs}, baseline_assertions({"price": theirs}))
    kinds = [(a.kind, a.min, a.max) for a in merged["price"].assertions]
    assert ("range", 1, 99) in kinds


def test_merging_does_not_duplicate_an_identical_assertion() -> None:
    spec = FieldSpec(
        name="price", required=True, type=TypeSpec(kind="scalar", value_type="price"),
        assertions=[Assertion(kind="not_empty")],
    )
    merged = with_assertions({"price": spec}, baseline_assertions({"price": spec}))
    assert [a.kind for a in merged["price"].assertions].count("not_empty") == 1


# --- parsing a proposal -----------------------------------------------------


def test_a_well_formed_proposal_is_kept() -> None:
    got = parse_assertions(
        {"fields": [{"field": "price", "assertions": [{"kind": "range", "min": 0, "max": 1e6}]}]},
        FIELDS,
    )
    assert got["price"][0].kind == "range"
    assert got["price"][0].max == 1e6


def test_an_uncompilable_regex_is_dropped() -> None:
    """`_evaluate_one` reports an invalid regex as a FAILURE, not a skip -- so
    storing one marks every future run suspect, forever."""

    got = parse_assertions(
        {"fields": [{"field": "title", "assertions": [{"kind": "matches", "regex": "(unclosed"}]}]},
        FIELDS,
    )
    assert got == {}


def test_an_assertion_missing_its_operand_is_dropped() -> None:
    """Same reason: a `range` with neither bound and a `matches` with no regex
    both fail every evaluation rather than passing vacuously."""

    got = parse_assertions(
        {
            "fields": [
                {"field": "price", "assertions": [{"kind": "range"}]},
                {"field": "title", "assertions": [{"kind": "matches"}]},
            ]
        },
        FIELDS,
    )
    assert got == {}


def test_an_empty_in_set_is_dropped() -> None:
    """`value in []` is false for every value."""

    got = parse_assertions(
        {"fields": [{"field": "title", "assertions": [{"kind": "in_set", "values": []}]}]},
        FIELDS,
    )
    assert got == {}


def test_the_mechanical_kinds_cannot_be_proposed() -> None:
    """`not_empty` and `cross_source_agrees` are added from the schema and the
    candidate chain. Letting the model also propose them invites duplicates and
    a `cross_source_agrees` on a field with only one source, which costs a
    second page read to learn nothing."""

    got = parse_assertions(
        {"fields": [{"field": "price", "assertions": [
            {"kind": "not_empty"}, {"kind": "cross_source_agrees"},
        ]}]},
        FIELDS,
    )
    assert got == {}


def test_a_field_the_schema_does_not_declare_is_ignored() -> None:
    got = parse_assertions(
        {"fields": [{"field": "invented", "assertions": [{"kind": "range", "min": 0}]}]},
        FIELDS,
    )
    assert got == {}
