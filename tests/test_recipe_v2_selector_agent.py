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
    _MAX_CORRECTABLE_RETRIES,
    _MAX_SNAPSHOT_CHARS,
    focus_snapshot,
    comma_group_reason,
    engine_only_selector_reason,
    is_correctable,
    scope_problem,
    shared_locator_failures,
    tautological_read,
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
    assert [s.locator.selector for s in survivors] == [".real"]
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

    async def __call__(
        self, fields, *, snapshot_text, structured_data, llm_config,
        failures=None, trace=None,
    ):
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


# --- locating a field inside a region a person pointed at --------------------


@pytest.mark.asyncio
async def test_a_scoped_proposal_is_bound_within_the_region(monkeypatch) -> None:
    """The person supplies the region -- the part they can see -- and the model
    supplies which node in it holds the value. `within` is what carries their
    half through to replay, and is why the binding survives a redesign of
    everything outside the section."""

    from agentpilot.recipe.v2.selector_agent import propose_within

    async def fake(messages, **kwargs):
        return {"fields": [{"field": "price", "candidates": [
            {"kind": "css", "selector": "dd.price"},
        ]}]}

    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.chat_json_conversation", fake)

    got = await propose_within(
        {"price": FIELDS["price"]},
        scope=Locator(kind="css", selector="#specs"),
        fragment_html="<dl><dd class=price>9550</dd></dl>",
        llm_config=None,
        verify=fake_page({("css", "dd.price"): "9550"}),
    )

    locator = got["price"][0].locator
    assert locator.selector == "dd.price"
    assert locator.within is not None
    assert locator.within.selector == "#specs"


@pytest.mark.asyncio
async def test_a_structured_candidate_is_not_scoped(monkeypatch) -> None:
    """A JSON path has no DOM container, so attaching one would make it
    unresolvable -- and those are the candidates worth keeping most."""

    from agentpilot.recipe.v2.selector_agent import propose_within

    async def fake(messages, **kwargs):
        return {"fields": [{"field": "price", "candidates": [
            {"kind": "hydration", "path": "props.price"},
        ]}]}

    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.chat_json_conversation", fake)

    got = await propose_within(
        {"price": FIELDS["price"]},
        scope=Locator(kind="css", selector="#specs"),
        fragment_html="<dl></dl>",
        llm_config=None,
        verify=fake_page({("hydration", "props.price"): "9550"}),
    )
    assert got["price"][0].locator.within is None


@pytest.mark.asyncio
async def test_a_scoped_proposal_that_does_not_resolve_is_dropped(monkeypatch) -> None:
    """A person pointing at the right region does not make a proposal correct.
    Binding one unchecked swaps a known gap for a silent one."""

    from agentpilot.recipe.v2.selector_agent import propose_within

    async def fake(messages, **kwargs):
        return {"fields": [{"field": "price", "candidates": [
            {"kind": "css", "selector": ".nope"},
        ]}]}

    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.chat_json_conversation", fake)

    got = await propose_within(
        {"price": FIELDS["price"]},
        scope=Locator(kind="css", selector="#specs"),
        fragment_html="<dl></dl>",
        llm_config=None,
        verify=fake_page({}),
    )
    assert got == {}


def test_the_scoped_prompt_warns_off_the_heading() -> None:
    """The failure this exists for: clicking "Specifications" and getting back
    the string "Specifications"."""

    from agentpilot.recipe.v2 import selector_agent as mod

    assert "is NOT the value" in mod._WITHIN_SYSTEM_PROMPT
    # Relative xpath, or the scope is ignored entirely.
    assert "Keep xpath RELATIVE" in mod._WITHIN_SYSTEM_PROMPT


# --- the transform is part of what is verified -------------------------------


@pytest.mark.asyncio
async def test_build_time_verification_agrees_with_replay() -> None:
    """THE guard. `verify_locators` and `resolve_field` must reach the same
    accept/reject decision for the same value, spec and locator.

    They did not, and that disagreement IS the bug: the build said "verified" on
    a raw read, replay applied the transform and got nothing, and the judge was
    the first thing in the pipeline to notice. A test is the only thing that
    keeps the two together as either changes.
    """

    from agentpilot.recipe.v2.resolve import resolve_field
    from agentpilot.recipe.v2.transform import TransformContext

    cases = {
        "₹ 9,550.00": True,          # cleans up to 9550.0
        "Contact us for pricing": False,  # reads fine, casts to nothing
        "   ": False,                # blank
    }
    spec = FIELDS["price"]
    ctx = TransformContext()

    for raw, expected in cases.items():
        locator = Locator(kind="css", selector=".p")
        survivors, _reason = await verify_locators(
            [locator], verify=fake_page({("css", ".p"): raw}), spec=spec, ctx=ctx
        )
        build_accepted = bool(survivors)

        async def evaluate(_loc, value=raw):
            return value

        resolution = await resolve_field(
            spec,
            [Candidate(locator=locator, transform=infer_transform(spec, locator))],
            evaluate=evaluate,
            ctx=ctx,
        )
        replay_accepted = resolution.status in ("resolved", "fallback")

        assert build_accepted == replay_accepted == expected, f"disagreed on {raw!r}"


@pytest.mark.asyncio
async def test_a_value_that_cleans_up_to_nothing_is_rejected_here() -> None:
    """"Contact us for pricing" read perfectly and verified perfectly, then
    became None at replay. Rejecting it here lets the next candidate be tried
    while the page is still open."""

    survivors, reason = await verify_locators(
        [Locator(kind="css", selector=".p")],
        verify=fake_page({("css", ".p"): "Contact us for pricing"}),
        spec=FIELDS["price"],
    )
    assert survivors == []
    # And the reason is one a model can act on -- it quotes what was read.
    assert "Contact us for pricing" in (reason or "")


@pytest.mark.asyncio
async def test_the_cleaned_value_is_returned_not_just_the_locator() -> None:
    """What the field will actually contain. Nothing at build time could see
    this before -- it existed only at replay, behind a judge."""

    survivors, _reason = await verify_locators(
        [Locator(kind="css", selector=".p")],
        verify=fake_page({("css", ".p"): "₹ 9,550.00"}),
        spec=FIELDS["price"],
    )
    assert survivors[0].raw == "₹ 9,550.00"
    assert survivors[0].value == 9550.0


@pytest.mark.asyncio
async def test_url_resolution_is_validated_against_the_page_url() -> None:
    """The case that silently passes if the context is dropped: a relative href
    joined against nothing stays relative, and looks fine."""

    from agentpilot.recipe.v2.transform import TransformContext

    spec = FieldSpec(name="link", type=TypeSpec(kind="scalar", value_type="url"))
    survivors, _reason = await verify_locators(
        [Locator(kind="css", selector="a", attribute="href")],
        verify=fake_page({("css", "a"): "/p/123"}),
        spec=spec,
        ctx=TransformContext(url="https://shop.test/c/shoes"),
    )
    assert survivors[0].value == "https://shop.test/p/123"


@pytest.mark.asyncio
async def test_an_assertion_failure_flags_and_does_not_reject() -> None:
    """An assertion the build guessed is a weaker claim than a value read off
    the page, so it annotates rather than discards."""

    from agentpilot.recipe.v2.schema import Assertion

    spec = FieldSpec(
        name="price",
        type=TypeSpec(kind="scalar", value_type="price"),
        assertions=[Assertion(kind="range", min=0)],
    )
    # Read from JSON, where the pipeline is a bare cast -- the DOM pipeline's
    # digit extraction would strip the sign before any assertion saw it.
    survivors, _reason = await verify_locators(
        [Locator(kind="json_ld", path="p")],
        verify=fake_page({("json_ld", "p"): "-5"}),
        spec=spec,
    )
    assert survivors[0].value == -5.0
    assert any("range" in note for note in survivors[0].notes)


@pytest.mark.asyncio
async def test_without_a_spec_the_raw_only_rule_is_kept() -> None:
    """Callers with no field to transform against are unaffected."""

    survivors, _reason = await verify_locators(
        [Locator(kind="css", selector=".p")],
        verify=fake_page({("css", ".p"): "Contact us for pricing"}),
    )
    assert len(survivors) == 1


# --- repairing the pipeline, not the selector --------------------------------


@pytest.mark.asyncio
async def test_a_transform_failure_asks_for_a_transform(monkeypatch) -> None:
    """The selector was right and the cleanup was wrong. Re-asking for a
    different locator returns another one onto the same kind of text, which
    fails the same way -- which is most of the loop."""

    stub = StubLLM([
        {"fields": [{"field": "price", "candidates": [{"kind": "css", "selector": ".p"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    asked: dict = {}

    async def fake_transform(messages, **kwargs):
        asked["user"] = messages[1]["content"]
        return {"transform": [
            {"op": "regex_replace", "pattern": "^Free$", "repl": "0"},
            {"op": "cast", "to": "price"},
        ]}

    monkeypatch.setattr(
        "agentpilot.recipe.v2.selector_agent.chat_json_conversation", fake_transform
    )

    got = await propose_and_verify(
        {"price": FIELDS["price"]},
        snapshot_text="", structured_data={},
        llm_config=None,
        # No digits, so the inferred `regex_extract` empties it -- the exact
        # shape of failure that used to reach the judge as "wrong value".
        verify=fake_page({("css", ".p"): "Free"}),
        dom_fallbacks=False,
    )

    # It was asked about the cleanup, with the text that broke it.
    assert "Free" in asked["user"]
    # And kept the selector, which was never the problem.
    assert got["price"][0].locator.selector == ".p"


@pytest.mark.asyncio
async def test_a_proposed_pipeline_is_run_before_it_is_accepted(monkeypatch) -> None:
    """The same propose-then-verify discipline this module applies to locators,
    applied to the other half of a binding."""

    # Two replies: the repair fails, so the ordinary retry runs once more.
    stub = StubLLM([
        {"fields": [{"field": "price", "candidates": [{"kind": "css", "selector": ".p"}]}]},
        {"fields": [{"field": "price", "candidates": [{"kind": "css", "selector": ".p"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    async def useless(messages, **kwargs):
        # Parses, runs, and still yields nothing on this text.
        return {"transform": [{"op": "regex_extract", "pattern": "zzz"}]}

    monkeypatch.setattr(
        "agentpilot.recipe.v2.selector_agent.chat_json_conversation", useless
    )

    got = await propose_and_verify(
        {"price": FIELDS["price"]},
        snapshot_text="", structured_data={},
        llm_config=None,
        verify=fake_page({("css", ".p"): "Contact us for pricing"}),
        dom_fallbacks=False,
    )
    assert got == {}


@pytest.mark.asyncio
async def test_declining_to_propose_a_pipeline_is_a_real_answer(monkeypatch) -> None:
    """"Contact us for pricing" is not a price in any cleanup. An empty list
    says so, and is more useful than a pipeline that cannot work."""

    from agentpilot.recipe.v2.selector_agent import propose_transform

    async def declines(messages, **kwargs):
        return {"transform": []}

    monkeypatch.setattr(
        "agentpilot.recipe.v2.selector_agent.chat_json_conversation", declines
    )
    got = await propose_transform(
        FIELDS["price"], "Contact us for pricing", llm_config=None
    )
    assert got is None


# --- the picked region's markup ---------------------------------------------


def test_pruning_keeps_the_values_and_the_selectable_attributes() -> None:
    """A region is only worth scoping to if the model gets to see all of it.

    A specifications section on a React page is mostly inline styles, generated
    class names and inert script tags, so a raw 20 000-character slice of
    `outerHTML` routinely cut off mid-table: the person pointed at the right
    region and the model was shown the first third of it.
    """

    from agentpilot.recipe.v2.selector_agent import prune_fragment

    raw = (
        '<section id="specs" class="dc_v3" style="padding:12px" '
        'data-testid="spec-block" onclick="track()" tabindex="-1">'
        "<!--$-->"
        "<script>window.__NEXT_DATA__={huge:true}</script>"
        "<style>.dc_v3{color:red}</style>"
        '<h2 aria-label="Specifications">Specifications</h2>'
        "<table><tr><th>Brand</th>"
        '<td itemprop="brand">Bodycology</td></tr></table>'
        '<svg viewBox="0 0 8 8"><path d="M0 0L8 8"/></svg>'
        "</section>"
    )
    out = prune_fragment(raw)

    # The values, and the relationships `_WITHIN_SYSTEM_PROMPT` asks for.
    assert "Bodycology" in out
    assert 'id="specs"' in out
    assert 'data-testid="spec-block"' in out
    assert 'itemprop="brand"' in out
    assert 'aria-label="Specifications"' in out
    assert "<table>" in out

    # None of what is never the answer.
    assert "__NEXT_DATA__" not in out
    assert "<script" not in out
    assert "<style" not in out
    assert "<svg" not in out
    assert "viewBox" not in out
    assert "onclick" not in out
    assert "style=" not in out
    assert "tabindex" not in out
    assert "<!--" not in out
    assert len(out) < len(raw)


def test_pruning_respects_the_fragment_budget() -> None:
    from agentpilot.recipe.v2.selector_agent import prune_fragment

    assert len(prune_fragment("<p>" + "x" * 5_000 + "</p>", limit=100)) == 100


def test_pruning_an_empty_fragment_is_not_an_error() -> None:
    """A person picked, then the page re-rendered. `propose_within` handles an
    empty region by returning no candidates; a throw would lose the batch."""

    from agentpilot.recipe.v2.selector_agent import prune_fragment

    assert prune_fragment("") == ""


# --- binding a list straight from the page's JSON ---------------------------


def _walmart_fixture() -> dict[str, Any]:
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "walmart_401967617_structured.json"
    return json.loads(path.read_text())


@pytest.mark.asyncio
async def test_a_list_field_binds_from_the_page_json_with_no_model_call(monkeypatch) -> None:
    """The `highlights` half of the bug report, on the page it failed on.

    An array the site itself names the way the caller named the field is not a
    guess. `propose_rows` has made this move for tables since it was written;
    a `list` field had no equivalent, so it was left guessing CSS at a page with
    no `<table>` and no `<tr>` -- and the path it needed was past the prompt's
    truncation point anyway.
    """

    from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

    images = FieldSpec(
        name="images",
        type=TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="url")),
    )
    url = (
        "__NEXT_DATA__.props.pageProps.initialData.data.product"
        ".imageInfo.allImages[*].url"
    )

    async def refuses(*args: Any, **kwargs: Any):
        raise AssertionError("the model must not be asked about a field the JSON answers")

    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", refuses)

    got = await propose_and_verify(
        {"images": images},
        snapshot_text="",
        structured_data=_walmart_fixture(),
        llm_config=None,
        verify=fake_page({("hydration", url): [
            "https://i5.walmartimages.com/asr/0.jpeg",
            "https://i5.walmartimages.com/asr/1.jpeg",
        ]}),
        dom_fallbacks=False,
    )

    assert [c.locator.path for c in got["images"]] == [url]
    assert got["images"][0].locator.path_lang == "jmespath"
    assert got["images"][0].locator.kind == "hydration"


@pytest.mark.asyncio
async def test_an_ambiguous_name_value_array_is_left_to_the_model(monkeypatch) -> None:
    """For a field called `highlights`, "Skin type" and "All" are each half of
    one fact, and nothing in the data says which half was asked for. Binding the
    winner of that tie would be a coin flip that looks like a measurement -- so
    the model is asked, with both readings and the page in front of it."""

    from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

    highlights = FieldSpec(
        name="highlights",
        description="Key product benefits, features, selling points, or bullet highlights",
        type=TypeSpec(kind="list", items=TypeSpec(kind="scalar", value_type="string")),
    )
    path = (
        "__NEXT_DATA__.props.pageProps.initialData.data.idml"
        ".productHighlights[*].value"
    )
    stub = StubLLM([{"fields": [
        {"field": "highlights", "candidates": [
            {"kind": "hydration", "path": path, "path_lang": "jmespath"},
        ]},
    ]}])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"highlights": highlights},
        snapshot_text="",
        structured_data=_walmart_fixture(),
        llm_config=None,
        verify=fake_page({("hydration", path): ["All", "Dryness", "Moisturizing"]}),
        dom_fallbacks=False,
    )

    # The model WAS asked, and both readings were offered to it -- which is the
    # whole reason it can answer at all.
    assert len(stub.prompts) == 1
    assert "productHighlights[*].name" in stub.prompts[0]
    assert "productHighlights[*].value" in stub.prompts[0]
    assert [c.locator.path for c in got["highlights"]] == [path]


def test_the_prompt_shows_the_paths_a_raw_prefix_would_have_cut(monkeypatch) -> None:
    """The `specifications` array sits ~40 KB into this page's blob. A 12 000-char
    prefix of `json.dumps` did not reach it, so the model was asked to write an
    anchored path into data it had never seen."""

    structured = _walmart_fixture()
    msg = build_user_message(FIELDS, snapshot_text="", structured_data=structured)

    import json as _j
    assert "idml.specifications" not in _j.dumps(structured)[:12_000]
    assert "idml.specifications" in msg
    # And never the sponsored competitor the page carries under `configs.ad`.
    assert "St. Ives" not in msg


# --- where the matches actually live ----------------------------------------


def scoped_page(values: dict[tuple, Any], scopes: dict[tuple, Any]):
    """A verifier that also reports where each locator's matches live, the way
    `PageReader.read_with_scope` does."""

    async def _probe(loc: Locator):
        key = (loc.kind, loc.selector or loc.path)
        return values.get(key), scopes.get(key)

    return _probe


CARE_FIELD = parse_fields({
    "care": {"type": {"kind": "list", "items": {"kind": "scalar", "value_type": "string"}}}
})["care"]

# What the page came back with on the build this exists for.
CHROME_AND_CARE = [
    "Bag0", "LOG IN", "Help", "FRUIT OF THE LOOMTHE NEWJACKETS",
    "Machine wash at max. 40ºC/104ºF with short spin cycle", "Do not use bleach",
]
CARE_ONLY = [
    "Machine wash at max. 40ºC/104ºF with short spin cycle", "Do not use bleach",
    "Iron at a maximum of 150ºC/302ºF",
]


@pytest.mark.asyncio
async def test_a_list_whose_matches_span_the_page_is_refused() -> None:
    """The Zara `care` failure, at the gate that now stops it.

    Every one of those six strings is a legitimate match for the expression. The
    only thing separating the answer from the site header is that they do not
    share a container -- so the nearest common ancestor of the whole set is
    <body>, and that is the measurement.
    """

    loc = Locator(kind="css", selector="li", all=True)
    resolving, reason = await verify_locators(
        [loc],
        verify=fake_page({}),
        spec=CARE_FIELD,
        ctx=TransformContext(),
        probe=scoped_page(
            {("css", "li"): CHROME_AND_CARE},
            {("css", "li"): {"spans_document": True, "tag": "body", "matched": 6}},
        ),
    )

    assert resolving == []
    assert reason is not None
    assert "spread across the whole page" in reason
    assert "<body>" in reason


@pytest.mark.asyncio
async def test_a_list_that_shares_a_container_is_scoped_to_it() -> None:
    """The same measurement, used the other way round: the container the values
    were found in is exactly the `within` the binding should carry."""

    loc = Locator(kind="css", selector="li", all=True)
    scope = {
        "spans_document": False, "tag": "ul", "matched": 3,
        "selector": "ul.care-list",
    }
    resolving, _reason = await verify_locators(
        [loc],
        verify=fake_page({}),
        spec=CARE_FIELD,
        ctx=TransformContext(),
        probe=scoped_page({("css", "li"): CARE_ONLY}, {("css", "li"): scope}),
    )

    assert len(resolving) == 1
    bound = resolving[0].locator
    assert bound.within is not None
    assert bound.within.selector == "ul.care-list"
    assert bound.selector == "li"


@pytest.mark.asyncio
async def test_a_scalar_is_never_scoped_to_itself() -> None:
    """A single match's "common ancestor" is the element. Scoping it to that
    would make the read resolve the container and then look for the element
    INSIDE it, which finds nothing -- and every scalar field takes this path."""

    loc = Locator(kind="css", selector="h1")
    scope = {"spans_document": False, "tag": "h1", "matched": 1, "selector": "h1.title"}
    resolving, _reason = await verify_locators(
        [loc],
        verify=fake_page({}),
        spec=FIELDS["name"],
        ctx=TransformContext(),
        probe=scoped_page({("css", "h1"): "Dove"}, {("css", "h1"): scope}),
    )

    assert len(resolving) == 1
    assert resolving[0].locator.within is None


@pytest.mark.asyncio
async def test_a_scope_that_changes_what_is_read_is_not_applied() -> None:
    """The scope selector resolves to `containers[0]`, and on a page carrying
    two same-shaped sections that need not be the one measured. So the scoped
    form is re-read and kept only if it still produces the same value."""

    loc = Locator(kind="css", selector="li", all=True)
    scope = {
        "spans_document": False, "tag": "ul", "matched": 3, "selector": "ul.list",
    }

    async def probe(loc: Locator):
        if loc.within is not None:
            return ["something", "else"], None   # a different `ul.list`
        return CARE_ONLY, scope

    resolving, _reason = await verify_locators(
        [loc], verify=fake_page({}), spec=CARE_FIELD,
        ctx=TransformContext(), probe=probe,
    )

    assert len(resolving) == 1
    assert resolving[0].locator.within is None
    assert resolving[0].value == CARE_ONLY


@pytest.mark.asyncio
async def test_without_a_probe_nothing_about_scoping_happens() -> None:
    """The old behaviour exactly, which is what lets every browser-free test
    here stay as it was."""

    loc = Locator(kind="css", selector="li", all=True)
    resolving, _reason = await verify_locators(
        [loc],
        verify=fake_page({("css", "li"): CHROME_AND_CARE}),
        spec=CARE_FIELD,
        ctx=TransformContext(),
    )
    assert len(resolving) == 1
    assert resolving[0].locator.within is None


# --- arity, in both directions ----------------------------------------------


@pytest.mark.asyncio
async def test_a_list_field_reading_one_value_is_refused() -> None:
    """The mirror of the scalar guard, and it was missing. A Zara build bound
    `highlights` -- declared a list -- to a selector with no `all`, so the
    recipe promised an array and produced a string."""

    resolving, reason = await verify_locators(
        [Locator(kind="css", selector=".expandable-text__inner-content p")],
        verify=fake_page({
            ("css", ".expandable-text__inner-content p"): "Midi dress made from viscose."
        }),
        spec=CARE_FIELD,
        ctx=TransformContext(),
    )

    assert resolving == []
    assert reason is not None
    assert "this field is a list" in reason
    assert "`all`" in reason


# --- an expression that cannot be contained ---------------------------------


def test_an_escaping_axis_never_becomes_a_candidate() -> None:
    """Dropped at parse time, so it never reaches the page at all."""

    raw = {"fields": [{"field": "care", "candidates": [
        {"kind": "xpath",
         "selector": "//*[contains(text(),'care')]/following::ul[1]/li", "all": True},
        {"kind": "xpath", "selector": ".//ul[@class='care-list']/li", "all": True},
    ]}]}
    got = parse_proposals(raw, {"care": CARE_FIELD})
    assert [loc.selector for loc in got["care"]] == [".//ul[@class='care-list']/li"]


@pytest.mark.asyncio
async def test_an_escaping_axis_arriving_any_other_way_is_still_refused() -> None:
    resolving, reason = await verify_locators(
        [Locator(kind="xpath", selector=".//p/following::ul/li", all=True)],
        verify=fake_page({("xpath", ".//p/following::ul/li"): CHROME_AND_CARE}),
        spec=CARE_FIELD,
        ctx=TransformContext(),
    )
    assert resolving == []
    assert "following::" in (reason or "")


# --- brittle beats nothing, and loses to better -----------------------------


@pytest.mark.asyncio
async def test_a_positional_chain_is_dropped_beside_a_good_selector(monkeypatch) -> None:
    stub = StubLLM([{"fields": [{"field": "name", "candidates": [
        {"kind": "css", "selector": "div > div > div > div > div > h1"},
        {"kind": "css", "selector": "[itemprop=name]"},
    ]}]}])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"name": FIELDS["name"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=fake_page({
            ("css", "div > div > div > div > div > h1"): "Dove",
            ("css", "[itemprop=name]"): "Dove",
        }),
        dom_fallbacks=False,
    )
    assert [c.locator.selector for c in got["name"]] == ["[itemprop=name]"]


@pytest.mark.asyncio
async def test_a_positional_chain_is_kept_when_it_is_all_there_is(monkeypatch) -> None:
    """A field with no binding collects nothing on every run, which is strictly
    worse than a selector that might rot."""

    chain = "div > div > div > div > div > h1"
    stub = StubLLM([{"fields": [
        {"field": "name", "candidates": [{"kind": "css", "selector": chain}]},
    ]}])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"name": FIELDS["name"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=fake_page({("css", chain): "Dove"}),
        dom_fallbacks=False,
    )
    assert [c.locator.selector for c in got["name"]] == [chain]


@pytest.mark.asyncio
async def test_a_weak_dom_selector_is_not_dropped_for_a_json_path(monkeypatch) -> None:
    """They are not competing on brittleness -- they are each other's
    insurance. `_add_dom_fallbacks` spends a whole extra model call to give a
    JSON-only field a DOM candidate, because a renamed hydration key fails
    silently and totally."""

    chain = "div > div > div > div > div > h1"
    stub = StubLLM([{"fields": [{"field": "name", "candidates": [
        {"kind": "json_ld", "path": "[0].name"},
        {"kind": "css", "selector": chain},
    ]}]}])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"name": FIELDS["name"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=fake_page({("json_ld", "[0].name"): "Dove", ("css", chain): "Dove"}),
        dom_fallbacks=False,
    )
    assert [c.locator.kind for c in got["name"]] == ["json_ld", "css"]


# --- two fields, one locator -------------------------------------------------
#
# Straight from a real Zara build. The agent clicked the "Composition, care &
# origin" accordion open, and the model then bound BOTH `care` and `origin` to
# the accordion's own button, so both fields collected the string
# "COMPOSITION, CARE & ORIGIN".
#
# Every per-field check passed: the locator resolved, it read a non-empty
# string, and the string cast cleanly to the declared type. The problem is the
# relationship between two fields, which nothing looking at one field at a time
# can see. `rows.py::_problems_with` has had the equivalent check for table
# columns since a build bound `material` and `percentage` to one json_ld path.

CARE_ORIGIN = parse_fields({
    "care": {"type": {"kind": "scalar", "value_type": "string"}, "description": "care instructions"},
    "origin": {"type": {"kind": "scalar", "value_type": "string"}, "description": "country of origin"},
})

_ACCORDION = {
    "kind": "ax_role", "role": "button",
    "name_contains": "Composition, care & origin",
}


def _ax_page(values: dict[tuple, Any]):
    """A verifier keyed on role/name, so an `ax_role` locator resolves."""

    async def _verify(loc: Locator) -> Any:
        if loc.kind == "ax_role":
            return values.get(("ax_role", loc.name_contains))
        return values.get((loc.kind, loc.selector or loc.path))

    return _verify


def test_shared_locator_failures_names_both_fields() -> None:
    both = Locator(**_ACCORDION)
    verified = {
        "care": [Candidate(locator=both)],
        "origin": [Candidate(locator=both)],
    }
    out = shared_locator_failures(verified)

    assert set(out) == {"care", "origin"}
    assert "'origin'" in out["care"]
    assert "'care'" in out["origin"]


def test_a_shared_fallback_is_fine() -> None:
    """Only the WINNING locator is compared. Later candidates are fallbacks
    that did not produce this value, and two fields may share one."""

    shared = Locator(kind="css", selector=".panel")
    verified = {
        "care": [Candidate(locator=Locator(kind="css", selector=".care")), Candidate(locator=shared)],
        "origin": [Candidate(locator=Locator(kind="css", selector=".origin")), Candidate(locator=shared)],
    }
    assert shared_locator_failures(verified) == {}


def test_distinct_locators_are_left_alone() -> None:
    verified = {
        "care": [Candidate(locator=Locator(kind="json_ld", path="[0].care"))],
        "origin": [Candidate(locator=Locator(kind="json_ld", path="[0].origin"))],
    }
    assert shared_locator_failures(verified) == {}


@pytest.mark.asyncio
async def test_two_fields_bound_to_one_element_are_both_re_asked(monkeypatch) -> None:
    """The regression. Round one gives both fields the accordion button; round
    two, told about the clash, finds the content inside it."""

    # Deliberately NOT the accordion button: that value would be refused by
    # `tautological_read` first, and this test is about the collision check.
    # One shared panel, whose text is nobody's search term.
    shared = {"kind": "css", "selector": ".panel"}
    stub = StubLLM([
        {"fields": [
            {"field": "care", "candidates": [dict(shared)]},
            {"field": "origin", "candidates": [dict(shared)]},
        ]},
        {"fields": [
            {"field": "care", "candidates": [{"kind": "css", "selector": ".care-body"}]},
            {"field": "origin", "candidates": [{"kind": "css", "selector": ".origin-body"}]},
        ]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        CARE_ORIGIN,
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({
            ("css", ".panel"): "Composition 100% polyester. Made in China.",
            ("css", ".care-body"): "Machine wash at 30",
            ("css", ".origin-body"): "Made in Portugal",
        }),
        max_retries=1,
        dom_fallbacks=False,
    )

    assert got["care"][0].locator.selector == ".care-body"
    assert got["origin"][0].locator.selector == ".origin-body"
    # The clash, not "did not resolve", is what it was told -- the locator DID
    # resolve, which is the whole reason nothing else caught this.
    assert "very same locator" in stub.prompts[1]


@pytest.mark.asyncio
async def test_a_collision_that_does_not_converge_binds_neither(monkeypatch) -> None:
    """An ask beats a confidently wrong value. Both fields read a real string
    that casts cleanly, so binding either would put the section heading into the
    dataset and nothing downstream would ever question it."""

    # Enough replies to exhaust the correctable-retry allowance too: a guard
    # rejection now buys another round, and the point of this test is what
    # happens when the model never takes the hint.
    same = {"fields": [
        {"field": "care", "candidates": [dict(_ACCORDION)]},
        {"field": "origin", "candidates": [dict(_ACCORDION)]},
    ]}
    stub = StubLLM([dict(same) for _ in range(2 + _MAX_CORRECTABLE_RETRIES)])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        CARE_ORIGIN,
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({
            ("ax_role", "Composition, care & origin"): "COMPOSITION, CARE & ORIGIN",
        }),
        max_retries=1,
        dom_fallbacks=False,
    )

    assert got == {}


@pytest.mark.asyncio
async def test_one_field_alone_on_a_locator_still_binds(monkeypatch) -> None:
    """The guard must not cost a field that simply has no rival."""

    stub = StubLLM([{"fields": [
        {"field": "care", "candidates": [dict(_ACCORDION)]},
    ]}])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"care": CARE_ORIGIN["care"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({("ax_role", "Composition, care & origin"): "Machine wash"}),
        dom_fallbacks=False,
    )

    assert got["care"][0].locator.role == "button"


# --- a locator that reads back its own search term ---------------------------
#
# How a real Zara build lost `care`. The agent scrolled the "Composition, care
# & origin" accordion into view; the model proposed
# `ax_role button name_contains="Composition, care & origin"`; that read the
# string "COMPOSITION, CARE & ORIGIN" -- non-empty, casts cleanly to the
# declared type -- so nothing objected and the field was FROZEN, twelve seconds
# before the click that opens the panel. Freezing removes a field from
# `unfound`, so when the real care text appeared one step later nothing was
# looking for it, and the recipe shipped reading the button's label for ever.
#
# From the run's own trace:
#   11:16:23  care -> {'role':'button','name_contains':'Composition, care & origin'}  frozen=['care']
#   11:16:35  route_step  op=click  target={'name_contains':'COMPOSITION, CARE & ORIGIN'}


def test_a_locator_reading_back_its_own_name_is_refused() -> None:
    loc = Locator(kind="ax_role", role="button", name_contains="Composition, care & origin")
    why = tautological_read(loc, "COMPOSITION, CARE & ORIGIN")

    assert why is not None
    assert "label of the element" in why


def test_case_and_whitespace_do_not_save_it() -> None:
    loc = Locator(kind="ax_role", role="button", name_contains="Composition, care & origin")
    assert tautological_read(loc, "  Composition,   care & origin ") is not None


def test_a_text_locator_is_held_to_the_same_rule() -> None:
    assert tautological_read(Locator(kind="text", text="Made in"), "Made in") is not None


def test_a_strict_superset_is_a_real_reading() -> None:
    """Matching on "Made in" and reading "Made in China" learned something the
    selector did not already contain. This must keep working -- it is how the
    same build correctly bound `origin`."""

    assert tautological_read(Locator(kind="text", text="Made in"), "Made in China") is None


def test_a_locator_that_matches_on_nothing_is_unaffected() -> None:
    loc = Locator(kind="css", selector=".care")
    assert tautological_read(loc, "Do not wash") is None


def test_a_non_string_read_is_unaffected() -> None:
    loc = Locator(kind="ax_role", role="button", name_contains="4")
    assert tautological_read(loc, 4) is None


@pytest.mark.asyncio
async def test_the_accordion_label_does_not_bind_the_field(monkeypatch) -> None:
    """End to end: the button's label is refused, so the field stays unfound and
    is still being looked for when the click reveals its real content."""

    stub = StubLLM([
        {"fields": [{"field": "care", "candidates": [
            {"kind": "ax_role", "role": "button",
             "name_contains": "Composition, care & origin"},
        ]}]},
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-body"},
        ]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"care": CARE_ORIGIN["care"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({
            ("ax_role", "Composition, care & origin"): "COMPOSITION, CARE & ORIGIN",
            ("css", ".care-body"): "Do not wash. Do not bleach.",
        }),
        max_retries=1,
        dom_fallbacks=False,
    )

    assert got["care"][0].locator.selector == ".care-body"
    assert "searched for" in stub.prompts[1]


# --- the same mistake, one step cleverer -------------------------------------
#
# Told that an exact round-trip is refused, the model shortened the needle until
# it was not one. From the run's own trace, all three fields on one button:
#
#   care:   {'role':'button','name_contains':'Composition, care'}
#   origin: {'role':'button','name_contains':'origin'}
#
# Both read back "COMPOSITION, CARE & ORIGIN". Neither is an exact echo, and
# both are the button's caption. What gives it away is that the value NAMES the
# field rather than answering it.


def test_a_shortened_needle_does_not_escape_the_check() -> None:
    loc = Locator(kind="ax_role", role="button", name_contains="Composition, care")
    why = tautological_read(loc, "COMPOSITION, CARE & ORIGIN", "care")

    assert why is not None
    assert "caption" in why


def test_a_single_word_needle_does_not_escape_either() -> None:
    loc = Locator(kind="ax_role", role="button", name_contains="origin")
    assert tautological_read(loc, "COMPOSITION, CARE & ORIGIN", "origin") is not None


def test_a_caption_is_refused_however_it_was_reached() -> None:
    """The regression that let the Zara `care` bug back in through another door.

    Both checks used to sit behind `if not needle: return None`, so a locator
    that reached the caption by CSS rather than by its text was never examined.
    `focus_snapshot` then made exactly that the likely proposal: once the
    snapshot budget followed the field's own vocabulary, the model was shown
    the region around the word "care" on a collapsed Zara page -- where the
    nearest element is the CleverCare badge -- and proposed
    `.product-detail-actions__clevercare` instead of an `ax_role` name match.
    Same caption, same freeze, same field lost for ever.

    Reading back your own search term needs a search term. *Being a caption* is
    a property of the value, not of how it was found.
    """

    css = Locator(kind="css", selector=".product-detail-actions__clevercare")
    why = tautological_read(css, "CLEVER CARE", "care")
    assert why is not None
    assert "caption" in why

    # And by xpath, which has no needle either.
    xpath = Locator(kind="xpath", selector="//div[@class='care']")
    assert tautological_read(xpath, "Care", "care") is not None


def test_a_needleless_locator_reading_real_content_still_binds() -> None:
    """The other half of the same change, and the one that keeps Ulta working.

    Widening the caption check to every locator kind must not start refusing
    CSS selectors that read actual content. An Ulta how-to-use panel is prose:
    it is past the caption length, so it is a value, not a label -- which is
    the whole reason length is the guard.
    """

    css = Locator(kind="css", selector=".pdp-how-to-use")
    prose = (
        "Apply an adequate amount to damp hair, massage gently into the scalp "
        "and rinse thoroughly. Repeat if necessary and follow with conditioner."
    )
    assert tautological_read(css, prose, "how_to_use") is None
    # Short, but it answers the field rather than naming it.
    assert tautological_read(css, "Made in China", "origin") is None


def test_a_caption_is_matched_on_word_boundaries() -> None:
    """`origin` must match "Composition, care & origin" but not "original
    price"; `care` must match "CARE" but not "careful"."""

    loc = Locator(kind="ax_role", role="button", name_contains="orig")
    assert tautological_read(loc, "Original price", "origin") is None
    assert tautological_read(loc, "Careful handling", "care") is None


def test_prose_that_mentions_the_field_name_is_not_a_caption() -> None:
    """A description containing the word "description" is a paragraph about the
    product, not a label for it. Length is what separates the two."""

    long_text = (
        "Full description: printed shoulder bag with an asymmetric top and a "
        "shoulder strap in a mix of materials, interior pocket and tie fastening."
    )
    loc = Locator(kind="text", text="Full description")
    assert tautological_read(loc, long_text, "description") is None


def test_a_real_value_from_a_matched_control_still_binds() -> None:
    """The check must not refuse every control. A button captioned with an
    actual value -- not with the field's name -- is a legitimate read."""

    loc = Locator(kind="ax_role", role="button", name_contains="Multicol")
    assert tautological_read(loc, "Multicoloured", "color") is None


def test_the_revealed_content_binds_normally() -> None:
    """Once the panel is open, the text inside it says nothing about the field's
    name and is accepted."""

    loc = Locator(kind="css", selector=".care-panel")
    assert tautological_read(loc, "Do not wash. Do not bleach.", "care") is None


# --- a correctable answer is not a dead end ----------------------------------
#
# A guard rejection and "that resolved to nothing" used to cost the same single
# retry. Measured on an Ulta product page with all three accordions already
# open: attempt one went on "this is 3 selectors joined by a comma", attempt two
# came back "model did not propose a locator for this field". It had been told
# its FORM was wrong, had no budget left to try the right one, and declined.
# Four fields failed that way in one batch, every one of them on the page.


def test_every_guard_message_is_recognised_as_correctable() -> None:
    """`is_correctable` matches on message text, so it has to be checked against
    the messages themselves -- otherwise editing a guard's wording silently
    turns its retry allowance off."""

    spec = FieldSpec(name="care")
    loc = Locator(kind="ax_role", role="button", name_contains="Composition, care & origin")

    messages = [
        tautological_read(loc, "Composition, care & origin", "care"),
        tautological_read(loc, "COMPOSITION, CARE & ORIGIN", "care"),
        comma_group_reason(Locator(kind="css", selector=".a, .b"), spec),
        engine_only_selector_reason(".x:has-text('y')"),
        shared_locator_failures({
            "care": [Candidate(locator=loc)],
            "origin": [Candidate(locator=loc)],
        })["care"],
        scope_problem({"spans_document": True, "matched": 9, "tag": "body"}, expects_many=True),
    ]
    for message in messages:
        assert message, "a guard produced no message"
        assert is_correctable(message), message[:70]


def test_a_dead_end_is_not_correctable() -> None:
    """"It resolved to nothing" tells the model nothing to change, so it must
    not buy another round."""

    for reason in (
        "read nothing",
        "no proposed candidate resolved to a value",
        "model did not propose a locator for this field",
    ):
        assert not is_correctable(reason), reason


@pytest.mark.asyncio
async def test_a_correctable_round_buys_another_attempt(monkeypatch) -> None:
    """With `max_retries=1` the comma rejection was the model's last word. It
    now gets to act on what it was told."""

    stub = StubLLM([
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-a, .care-b"},
        ]}]},
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-body"},
        ]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"care": CARE_ORIGIN["care"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({("css", ".care-body"): "Do not wash"}),
        max_retries=0,          # no ordinary retry at all
        dom_fallbacks=False,
    )

    assert got["care"][0].locator.selector == ".care-body"
    assert "joined by a comma" in stub.prompts[1]


@pytest.mark.asyncio
async def test_a_dead_end_does_not_buy_one(monkeypatch) -> None:
    """The allowance is for feedback the model can act on, not for retrying a
    field the page will not yield."""

    stub = StubLLM([
        {"fields": [{"field": "care", "candidates": [{"kind": "css", "selector": ".nope"}]}]},
        {"fields": [{"field": "care", "candidates": [{"kind": "css", "selector": ".nope2"}]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"care": CARE_ORIGIN["care"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({}), max_retries=0, dom_fallbacks=False,
    )

    assert got == {}
    assert len(stub.prompts) == 1, "a dead end must not cost a second model call"


@pytest.mark.asyncio
async def test_one_correctable_field_earns_the_round_for_the_batch(monkeypatch) -> None:
    """A batch asks about several fields at once and they fail for different
    reasons. Requiring every one to be correctable meant the allowance never
    fired: measured on an Ulta page where four fields failed together, two with
    "joined by a comma" and two with "did not propose a locator", and the round
    was denied to both. The retry is one shared model call."""

    stub = StubLLM([
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-a, .care-b"},   # correctable
        ]}]},                                                   # origin: absent -> dead end
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-body"},
        ]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        CARE_ORIGIN,
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({("css", ".care-body"): "Do not wash"}),
        max_retries=0, dom_fallbacks=False,
    )

    assert got["care"][0].locator.selector == ".care-body"


@pytest.mark.asyncio
async def test_the_allowance_fires_at_the_production_retry_setting(monkeypatch) -> None:
    """`max_retries=1` is what `onboard` actually uses, and the first version of
    this only granted the round when the budget was already spent -- by which
    point the model answers "did not propose a locator", which is not
    correctable. The allowance never fired once in production; the correctable
    answer arrives on the FIRST round and that is the one that must buy the
    next."""

    stub = StubLLM([
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-a, .care-b"},
        ]}]},
        {"fields": []},                                    # gives up
        {"fields": [{"field": "care", "candidates": [
            {"kind": "css", "selector": ".care-body"},
        ]}]},
    ])
    monkeypatch.setattr("agentpilot.recipe.v2.selector_agent.propose_locators", stub)

    got = await propose_and_verify(
        {"care": CARE_ORIGIN["care"]},
        snapshot_text="", structured_data={}, llm_config=None,
        verify=_ax_page({("css", ".care-body"): "Do not wash"}),
        max_retries=1, dom_fallbacks=False,
    )

    assert got["care"][0].locator.selector == ".care-body"
    assert len(stub.prompts) == 3, "the comma rejection must buy a further round"


# --- the snapshot budget follows the fields, not document order --------------


def _long_page(tail_lines: list[str]) -> str:
    head = "\n".join(f"\t<div class=nav>\n\t\tNav promo {i}" for i in range(2600))
    return head + "\n" + "\n".join(tail_lines)


def test_the_budget_reaches_content_a_head_prefix_never_would() -> None:
    """A head-first prefix spends the budget in document order, and on a
    commerce page document order is the header.

    Measured on an Ulta product page: the render is 72 000 characters, the cap
    is 24 000, so the model saw the first third -- "SKIP TO MAIN", "Join / Sign
    in", "Track an Order" -- and none of the accordion content it was asked to
    locate. It declined every field, correctly, because from where it stood the
    page did not have them.
    """

    page = _long_page([
        '[e13]<button "Ingredients" aria-label=Ingredients />',
        "<div class=Markdown data-test=markdown>",
        "\tAqua (Water), Melaleuca Alternifolia Leaf Water, Propanediol.",
    ])
    fields = {
        "ingredients": FieldSpec(
            name="ingredients", description="Complete ingredient list as displayed"
        )
    }
    _ = fields

    assert "Ingredients" not in page[:_MAX_SNAPSHOT_CHARS]
    focused = focus_snapshot(page, fields)
    assert "Ingredients" in focused
    assert len(focused) <= _MAX_SNAPSHOT_CHARS


def test_the_head_is_kept_for_the_pages_identity() -> None:
    """The title, the price and the breadcrumb live there."""

    page = _long_page(["\tIngredients listed here"])
    focused = focus_snapshot(page, {"ingredients": FieldSpec(name="ingredients", description="ingredient list")})
    assert "Nav promo 0" in focused


def test_elisions_are_marked() -> None:
    """So the model knows it is reading an excerpt rather than the end of the
    page -- the mistake the agent-loop truncation used to invite."""

    page = _long_page(["\tIngredients listed here"])
    focused = focus_snapshot(page, {"ingredients": FieldSpec(name="ingredients", description="ingredient list")})
    assert "lines not shown" in focused


def test_a_snapshot_inside_the_budget_is_untouched() -> None:
    page = "<div class=a>\n\tsmall page"
    assert focus_snapshot(page, {"x": FieldSpec(name="x", description="thing")}) == page


def test_nothing_matching_falls_back_to_the_plain_prefix() -> None:
    """A field's vocabulary and its content's need not overlap at all -- "Apply
    an adequate amount" shares no word with "Directions or instructions for
    using". When the windows find nothing this must be no worse than not
    having it."""

    page = _long_page(["\tApply an adequate amount evenly."])
    fields = {"how_to_use": FieldSpec(name="how_to_use", description="Directions for using")}
    assert focus_snapshot(page, fields) == page[:_MAX_SNAPSHOT_CHARS]

    assert focus_snapshot(page, {"a": FieldSpec(name="a", description="the of and")}) == page[:_MAX_SNAPSHOT_CHARS]
