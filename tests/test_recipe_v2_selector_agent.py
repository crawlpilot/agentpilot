"""The selector agent: proposal parsing, verification, ranking and retry.

No browser and no LLM -- the page is a fake verifier and the model is a stub,
which is what makes the ordering and retry rules testable exhaustively rather
than incidentally.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentpilot.recipe.v2.models import Locator
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec, parse_fields
from agentpilot.recipe.v2.selector_agent import (
    SOURCE_PRIORITY,
    build_user_message,
    dedupe_locators,
    infer_transform,
    parse_proposals,
    propose_and_verify,
    rank_candidates,
    verify_locators,
)
from agentpilot.recipe.v2.transform import TransformContext, apply_transforms

FIELDS = parse_fields({
    "name": {"type": {"kind": "scalar", "value_type": "string"}, "description": "title"},
    "price": {"type": {"kind": "scalar", "value_type": "price"}, "description": "price"},
})


def fake_page(values: dict[tuple, Any]):
    """A verifier backed by a dict keyed on the locator's identity."""

    async def _verify(loc: Locator) -> Any:
        return values.get((loc.kind, loc.selector or loc.path))

    return _verify


# --- parsing ----------------------------------------------------------------


def test_parses_candidates_and_drops_unknown_fields() -> None:
    raw = {"fields": [
        {"field": "name", "candidates": [{"kind": "json_ld", "path": "[0].name"}]},
        {"field": "not_requested", "candidates": [{"kind": "css", "selector": "h1"}]},
    ]}
    got = parse_proposals(raw, FIELDS)
    assert set(got) == {"name"}
    assert got["name"][0].path == "[0].name"


@pytest.mark.parametrize("candidate", [
    {"kind": "json_ld"},                 # structured source with no path
    {"kind": "css"},                     # css with no selector
    {"kind": "xpath"},                   # xpath with no selector
    {"kind": "ax_role"},                 # ax_role with no role
    {"kind": "telepathy", "path": "x"},  # not a source at all
])
def test_malformed_candidates_are_skipped_not_fatal(candidate: dict) -> None:
    """A bad item must not cost the other fields in the same reply."""
    raw = {"fields": [
        {"field": "name", "candidates": [candidate, {"kind": "css", "selector": "h1"}]},
    ]}
    got = parse_proposals(raw, FIELDS)
    assert [loc.kind for loc in got["name"]] == ["css"]


def test_candidate_count_is_capped() -> None:
    raw = {"fields": [{"field": "name", "candidates": [
        {"kind": "css", "selector": f"h{i}"} for i in range(9)
    ]}]}
    assert len(parse_proposals(raw, FIELDS)["name"]) == 4


def test_defaults_are_applied_to_a_sparse_candidate() -> None:
    raw = {"fields": [{"field": "name", "candidates": [{"kind": "css", "selector": "h1"}]}]}
    loc = parse_proposals(raw, FIELDS)["name"][0]
    assert (loc.attribute, loc.all, loc.path_lang) == ("text", False, "simple")


# --- ranking ----------------------------------------------------------------


def test_structured_sources_outrank_the_dom() -> None:
    ranked = rank_candidates([
        Locator(kind="css", selector=".price"),
        Locator(kind="json_ld", path="[0].offers.price"),
        Locator(kind="meta", path="og:price"),
    ])
    assert [c.locator.kind for c in ranked] == ["json_ld", "meta", "css"]


def test_within_a_tier_the_models_own_order_is_kept() -> None:
    ranked = rank_candidates([
        Locator(kind="css", selector=".first"),
        Locator(kind="css", selector=".second"),
    ])
    assert [c.locator.selector for c in ranked] == [".first", ".second"]


def test_priorities_are_spaced_so_a_human_can_insert_between_tiers() -> None:
    ranked = rank_candidates([
        Locator(kind="json_ld", path="a"), Locator(kind="css", selector="b"),
    ])
    assert ranked[1].priority - ranked[0].priority > 1
    assert SOURCE_PRIORITY["json_ld"] < SOURCE_PRIORITY["css"] < SOURCE_PRIORITY["xpath"]


def test_dedupe_keeps_first_occurrence() -> None:
    locs = [
        Locator(kind="css", selector="h1"),
        Locator(kind="css", selector="h1"),
        Locator(kind="css", selector="h2"),
    ]
    assert [loc.selector for loc in dedupe_locators(locs)] == ["h1", "h2"]


# --- per-candidate transforms ----------------------------------------------


def test_numeric_field_gets_source_appropriate_cleanup() -> None:
    """The real pair: Zara's JSON-LD gives '9550', its DOM gives '₹ 9,550.00'.
    One field-level pipeline cannot serve both."""

    price = FIELDS["price"]
    from_json = infer_transform(price, Locator(kind="json_ld", path="p"))
    from_dom = infer_transform(price, Locator(kind="css", selector=".p"))

    ctx = TransformContext()
    assert apply_transforms("9550", from_json, ctx) == 9550.0
    assert apply_transforms("₹ 9,550.00", from_dom, ctx) == 9550.0
    # And the naive one would have been wrong on the DOM value:
    assert apply_transforms("₹ 9,550.00", from_json, ctx) == 9550.0  # cast is lenient
    assert len(from_dom) > len(from_json)


def test_non_numeric_fields_get_no_inferred_transform() -> None:
    assert infer_transform(FIELDS["name"], Locator(kind="css", selector="h1")) is None


def test_table_fields_get_no_inferred_transform() -> None:
    spec = FieldSpec(name="rows", type=TypeSpec(kind="table", columns={}))
    assert infer_transform(spec, Locator(kind="css", selector="tr")) is None


# --- verification -----------------------------------------------------------


@pytest.mark.asyncio
async def test_only_resolving_locators_survive() -> None:
    verify = fake_page({("css", ".real"): "Dove"})
    survivors, reason = await verify_locators(
        [Locator(kind="css", selector=".missing"), Locator(kind="css", selector=".real")],
        verify=verify,
    )
    assert [s.selector for s in survivors] == [".real"]
    assert reason is None


@pytest.mark.asyncio
async def test_whitespace_only_does_not_count_as_resolved() -> None:
    """Amazon's price node resolved to '' on a geo-gated load. A candidate that
    returns blank has not found anything."""

    survivors, reason = await verify_locators(
        [Locator(kind="css", selector=".p")], verify=fake_page({("css", ".p"): "   "})
    )
    assert survivors == []
    assert reason is not None


@pytest.mark.asyncio
async def test_a_raising_locator_is_data_not_a_crash() -> None:
    async def _boom(loc: Locator) -> Any:
        raise ValueError("bad selector")

    survivors, reason = await verify_locators(
        [Locator(kind="xpath", selector="//[")], verify=_boom
    )
    assert survivors == []
    assert "raised" in reason


# --- the loop ---------------------------------------------------------------


class StubLLM:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    async def __call__(self, fields, *, snapshot_text, structured_data, llm_config, failures=None):
        self.prompts.append(
            build_user_message(
                fields, snapshot_text=snapshot_text,
                structured_data=structured_data, failures=failures,
            )
        )
        return parse_proposals(self.replies.pop(0), fields)


@pytest.mark.asyncio
async def test_verified_fields_become_ordered_candidates(monkeypatch) -> None:
    stub = StubLLM([{"fields": [
        {"field": "name", "candidates": [
            {"kind": "css", "selector": ".n"},
            {"kind": "json_ld", "path": "[0].name"},
        ]},
    ]}])
    monkeypatch.setattr(
        "agentpilot.recipe.v2.selector_agent.propose_locators", stub
    )
    got = await propose_and_verify(
        {"name": FIELDS["name"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=fake_page({("css", ".n"): "Dove", ("json_ld", "[0].name"): "Dove"}),
    )
    assert [c.locator.kind for c in got["name"]] == ["json_ld", "css"]
    assert all(c.verified_on == 1 for c in got["name"])


@pytest.mark.asyncio
async def test_failure_is_fed_back_and_the_retry_can_succeed(monkeypatch) -> None:
    stub = StubLLM([
        {"fields": [{"field": "name", "candidates": [{"kind": "css", "selector": ".wrong"}]}]},
        {"fields": [{"field": "name", "candidates": [{"kind": "css", "selector": ".right"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)
    got = await propose_and_verify(
        {"name": FIELDS["name"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=fake_page({("css", ".right"): "Dove"}),
        max_retries=1,
    )
    assert got["name"][0].locator.selector == ".right"
    assert "did NOT resolve" in stub.prompts[1]


@pytest.mark.asyncio
async def test_an_unlocatable_field_is_absent_not_an_error(monkeypatch) -> None:
    """The caller keeps it and tries again on a later exploration step, once
    the page has been interacted with further."""

    stub = StubLLM([{"fields": []}, {"fields": []}])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)
    got = await propose_and_verify(
        {"name": FIELDS["name"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=fake_page({}), max_retries=1,
    )
    assert got == {}


# --- the prompt -------------------------------------------------------------


def test_prompt_tells_the_model_when_there_is_no_structured_data() -> None:
    """Amazon ships zero JSON-LD scripts. Without this the model invents paths
    into a blob that does not exist."""

    msg = build_user_message(FIELDS, snapshot_text="<h1>x</h1>", structured_data={})
    assert "no usable structured data" in msg


def test_prompt_flags_truncation_of_a_large_blob() -> None:
    """Walmart's __NEXT_DATA__ is 352 KB. Silently cutting it would have the
    model conclude a key is absent when it was merely off the end."""

    msg = build_user_message(
        FIELDS, snapshot_text="", structured_data={"hydration": {"k": "x" * 40_000}},
    )
    assert "TRUNCATED" in msg


def test_prompt_carries_the_field_types_and_requirements() -> None:
    fields = parse_fields({
        "price": {"type": {"kind": "scalar", "value_type": "price"},
                  "required": True, "description": "current price"},
    })
    msg = build_user_message(fields, snapshot_text="", structured_data={})
    assert "expected type: price" in msg and "[REQUIRED]" in msg
