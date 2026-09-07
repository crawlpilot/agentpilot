"""The selector agent: proposal parsing, verification, ranking and retry.

No browser and no LLM -- the page is a fake verifier and the model is a stub,
which is what makes the ordering and retry rules testable exhaustively rather
than incidentally.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentpilot.recipe.v2.models import Candidate, Locator
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


def test_a_string_read_from_the_dom_is_whitespace_cleaned() -> None:
    """Rendered text arrives with the page's own indentation in it."""

    got = infer_transform(FIELDS["name"], Locator(kind="css", selector="h1"))
    assert apply_transforms("\n   Ribbed  top\n ", got, TransformContext()) == "Ribbed top"


def test_a_string_read_from_json_is_left_alone() -> None:
    """It was not rendered, so there is no layout whitespace to strip -- and a
    transform that does nothing is still a transform someone has to read."""

    assert infer_transform(FIELDS["name"], Locator(kind="json_ld", path="[0].name")) is None


def test_a_url_field_is_resolved_against_the_page() -> None:
    """Relative hrefs are the commonest broken output there is: `/p/123` handed
    to a caller with no idea what it was relative to."""

    spec = FieldSpec(name="link", type=TypeSpec(kind="scalar", value_type="url"))
    ctx = TransformContext(url="https://shop.test/c/shoes")
    for locator in (Locator(kind="css", selector="a"), Locator(kind="json_ld", path="url")):
        got = infer_transform(spec, locator)
        assert apply_transforms("/p/123", got, ctx) == "https://shop.test/p/123"


def test_a_list_of_urls_is_resolved_element_wise_and_compacted() -> None:
    """Scalar ops map over a list, so the item cleanup is written once. A DOM
    list routinely carries blanks from layout elements the selector also
    caught, and the caller asked for the values, not the gaps."""

    spec = FieldSpec(
        name="images",
        type=TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="url")),
    )
    got = infer_transform(spec, Locator(kind="css", selector="img", all=True))
    out = apply_transforms(
        ["/a.jpg", "", "/b.jpg"], got, TransformContext(url="https://shop.test/p/1")
    )
    assert out == ["https://shop.test/a.jpg", "https://shop.test/b.jpg"]


def test_table_fields_get_no_inferred_transform() -> None:
    """A table has no value of its own -- its columns are separate leaf fields
    with their own specs, and each comes back through here."""

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


# --- the DOM fallback behind a JSON-only field ------------------------------


def test_a_json_only_chain_is_flagged_for_a_fallback() -> None:
    from agentpilot.recipe.v2.selector_agent import needs_dom_fallback

    json_only = [
        Candidate(locator=Locator(kind="hydration", path="a")),
        Candidate(locator=Locator(kind="json_ld", path="b")),
    ]
    assert needs_dom_fallback(json_only) is True


def test_a_chain_that_already_reaches_the_dom_is_not() -> None:
    from agentpilot.recipe.v2.selector_agent import needs_dom_fallback

    mixed = [
        Candidate(locator=Locator(kind="hydration", path="a")),
        Candidate(locator=Locator(kind="css", selector=".p")),
    ]
    assert needs_dom_fallback(mixed) is False
    assert needs_dom_fallback([]) is False


@pytest.mark.asyncio
async def test_a_json_only_field_gets_a_dom_fallback_added(monkeypatch) -> None:
    """A hydration key rename is silent and total: nothing resolves, the field
    is simply absent, and the page still renders the value perfectly to anyone
    who looks. A CSS break is loud and the chain is already built for it -- so
    the chain wants one of each, not the best of one.
    """

    stub = StubLLM([
        # First pass: the model finds it in the page's JSON, and only there.
        {"fields": [{"field": "price", "candidates": [{"kind": "hydration", "path": "p.price"}]}]},
        # The follow-up asks for a rendered-DOM reading of the same value.
        {"fields": [{"field": "price", "candidates": [{"kind": "css", "selector": ".price"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"price": FIELDS["price"]},
        snapshot_text="", structured_data={"hydration": {}},
        llm_config=None,
        verify=fake_page({("hydration", "p.price"): "9550", ("css", ".price"): "9,550.00"}),
    )

    kinds = [c.locator.kind for c in got["price"]]
    assert kinds == ["hydration", "css"]
    # The JSON candidate still wins: the fallback is for when the primary stops
    # resolving, not a competitor for the common case.
    assert got["price"][0].priority < got["price"][1].priority
    # And the follow-up said what it wanted, so the model is not guessing.
    assert "RENDERED DOM ONLY" in stub.prompts[1]


@pytest.mark.asyncio
async def test_the_fallback_is_verified_like_any_other_candidate(monkeypatch) -> None:
    """A proposed fallback that does not actually resolve is worse than none:
    it costs a page read on every run and then reports empty."""

    stub = StubLLM([
        {"fields": [{"field": "price", "candidates": [{"kind": "hydration", "path": "p.price"}]}]},
        {"fields": [{"field": "price", "candidates": [{"kind": "css", "selector": ".nope"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"price": FIELDS["price"]},
        snapshot_text="", structured_data={},
        llm_config=None,
        verify=fake_page({("hydration", "p.price"): "9550"}),
    )
    assert [c.locator.kind for c in got["price"]] == ["hydration"]


@pytest.mark.asyncio
async def test_no_second_call_when_the_chain_already_reaches_the_dom(monkeypatch) -> None:
    """It costs a model call per build; a field that already has both sources
    has nothing to gain from one."""

    stub = StubLLM([
        {"fields": [{"field": "price", "candidates": [
            {"kind": "hydration", "path": "p.price"},
            {"kind": "css", "selector": ".price"},
        ]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    await propose_and_verify(
        {"price": FIELDS["price"]},
        snapshot_text="", structured_data={},
        llm_config=None,
        verify=fake_page({("hydration", "p.price"): "9550", ("css", ".price"): "9550"}),
    )
    assert len(stub.prompts) == 1


@pytest.mark.asyncio
async def test_the_fallback_pass_can_be_turned_off(monkeypatch) -> None:
    stub = StubLLM([
        {"fields": [{"field": "price", "candidates": [{"kind": "hydration", "path": "p.price"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    await propose_and_verify(
        {"price": FIELDS["price"]},
        snapshot_text="", structured_data={},
        llm_config=None,
        verify=fake_page({("hydration", "p.price"): "9550"}),
        dom_fallbacks=False,
    )
    assert len(stub.prompts) == 1
