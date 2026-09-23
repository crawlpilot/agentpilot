"""What a build tried, and why each attempt was rejected.

The expensive part of this already existed: `verify_locators` and
`rows._problems_with` compute their rejection strings and write them to be read
by a person. They went to a worker's stdout, so "it is failing" was a report
about a run whose reasoning had already been discarded. These tests are mostly
about that -- the reasons arriving intact, and the collector staying inert when
nobody asked for one.
"""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2.build_trace import BuildTrace, record, show
from agentpilot.recipe.v2.models import Locator
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.selector_agent import verify_locators
from agentpilot.recipe.v2.transform import TransformContext


def _fake_page(values: dict[tuple, object]):
    """A verifier backed by a dict, keyed the way `test_recipe_v2_selector_agent`
    keys it -- no browser, which is the point of `Verifier` being injected."""

    async def verify(loc: Locator):
        return values.get((loc.kind, loc.selector or loc.path))

    return verify


# --- the collector ----------------------------------------------------------


def test_recording_is_a_no_op_without_a_collector() -> None:
    """Every call site is a single unconditional line, so tracing can be off
    without the code doing the work knowing about it -- and so every existing
    browser-free test keeps passing with no trace at all."""

    record(None, "price", "propose", "rejected", reason="nope")  # must not raise


def test_a_trace_keeps_the_order_things_were_tried_in() -> None:
    trace = BuildTrace()
    record(trace, "price", "page_json", "rejected", reason="read nothing")
    record(trace, "price", "propose", "bound", reason=None)

    assert [(e.field, e.stage, e.outcome) for e in trace.entries] == [
        ("price", "page_json", "rejected"),
        ("price", "propose", "bound"),
    ]


def test_a_trace_is_bounded() -> None:
    """A build proposes a handful of locators per field. Past the cap something
    is looping, and a trace that grows without limit is a second bug rather than
    a diagnosis of the first."""

    trace = BuildTrace()
    for _ in range(500):
        record(trace, "price", "propose", "rejected", reason="x")

    assert len(trace.entries) == 400
    assert trace.dropped == 100
    assert trace.to_dict()["dropped"] == 100


def test_a_read_is_shortened_but_still_identifiable() -> None:
    """A read can be a whole rendered table. The point of recording it is to see
    what the selector grabbed, and the first line of that is enough to tell a
    spec sheet from a recommended-products carousel."""

    assert show("  a\n  b  ") == "a b"
    long = show(["x" * 1000])
    assert len(long) < 450
    assert long.endswith("...")
    assert show({"a": 1}) == '{"a": 1}'
    # `default=str` means nothing a page can return is unrepresentable, so a
    # trace never loses an entry to a value it could not describe.
    assert "object object at" in show(object())


def test_explain_says_what_was_ruled_out_for_one_field() -> None:
    trace = BuildTrace()
    record(trace, "specs", "rows", "rejected", reason="every one of the 10 rows was identical")
    record(trace, "specs", "propose", "rejected", reason="read 'Specifications'")
    record(trace, "price", "propose", "rejected", reason="not this field")
    record(trace, "specs", "propose", "bound", reason=None)

    explained = trace.explain("specs")
    assert "every one of the 10 rows was identical" in explained
    assert "read 'Specifications'" in explained
    assert "not this field" not in explained
    # A bound attempt is not something that was ruled out.
    assert explained.count("\n") == 1


def test_explain_does_not_repeat_itself() -> None:
    """Four candidates failing the same way is one thing to tell a person, not
    four."""

    trace = BuildTrace()
    for _ in range(4):
        record(trace, "price", "propose", "rejected", reason="read nothing")
    assert trace.explain("price") == "propose: read nothing"


def test_explain_is_empty_for_a_field_that_worked() -> None:
    trace = BuildTrace()
    record(trace, "price", "propose", "bound")
    assert trace.explain("price") == ""


# --- the integration, which is where the useful strings come from -----------


@pytest.mark.asyncio
async def test_the_scalar_guards_reason_reaches_the_trace() -> None:
    """The string that names how a table's columns became parallel arrays. It
    was already being written; now it is kept."""

    spec = FieldSpec(name="title", type=TypeSpec(kind="scalar", value_type="string"))
    loc = Locator(kind="css", selector=".t", all=True)
    trace = BuildTrace()

    resolving, reason = await verify_locators(
        [loc],
        verify=_fake_page({("css", ".t"): ["one", "two", "three"]}),
        spec=spec,
        ctx=TransformContext(),
        trace=trace,
    )

    assert resolving == []
    assert "reads 3 values but this field is one value" in (reason or "")
    entry = trace.entries[0]
    assert entry.field == "title"
    assert entry.outcome == "rejected"
    assert entry.reason == reason
    # What it actually grabbed, which is the difference between a selector that
    # found nothing and one that found the wrong thing.
    assert entry.read is not None and "one" in entry.read
    assert entry.locator == loc.to_dict()


@pytest.mark.asyncio
async def test_a_read_that_cleans_up_to_nothing_is_traced_with_its_pipeline() -> None:
    """"Contact us for pricing" is not a price in any cleanup. An empty read
    after a pipeline is a different problem from an empty read before one, and
    the pipeline is what says which."""

    spec = FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="price"))
    trace = BuildTrace()

    resolving, reason = await verify_locators(
        [Locator(kind="css", selector=".p")],
        verify=_fake_page({("css", ".p"): "Contact us for pricing"}),
        spec=spec,
        ctx=TransformContext(),
        trace=trace,
    )

    assert resolving == []
    assert "cleaning it up left nothing" in (reason or "")
    assert trace.entries[0].transform  # the pipeline that was actually run
    assert "Contact us" in (trace.entries[0].read or "")


@pytest.mark.asyncio
async def test_an_empty_read_is_traced_even_though_it_is_not_the_reason() -> None:
    """An empty read is the ordinary "not on this page" and says nothing a model
    could act on, so it is deliberately not fed back as the failure reason. But
    "every candidate read nothing" and "one read the wrong thing" are different
    diagnoses, and only the trace can tell them apart."""

    spec = FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="string"))
    trace = BuildTrace()

    resolving, reason = await verify_locators(
        [Locator(kind="css", selector=".missing")],
        verify=_fake_page({}),
        spec=spec,
        ctx=TransformContext(),
        trace=trace,
    )

    assert resolving == []
    assert reason == "no proposed candidate resolved to a value"
    assert [e.reason for e in trace.entries] == ["read nothing"]


@pytest.mark.asyncio
async def test_the_stage_travels_with_the_attempt() -> None:
    """Which stage rejected a field is most of the diagnosis: "the deterministic
    JSON pass found nothing" and "the model proposed nothing" need opposite
    fixes."""

    spec = FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="string"))
    trace = BuildTrace()

    await verify_locators(
        [Locator(kind="hydration", path="a.b")],
        verify=_fake_page({("hydration", "a.b"): "4.97"}),
        spec=spec,
        ctx=TransformContext(),
        trace=trace,
        stage="page_json",
    )
    assert trace.entries[0].stage == "page_json"
    assert trace.entries[0].outcome == "bound"


@pytest.mark.asyncio
async def test_verification_without_a_trace_behaves_exactly_as_before() -> None:
    spec = FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="string"))
    resolving, reason = await verify_locators(
        [Locator(kind="css", selector=".p")],
        verify=_fake_page({("css", ".p"): "4.97"}),
        spec=spec,
        ctx=TransformContext(),
    )
    assert [v.value for v in resolving] == ["4.97"]
    assert reason is None
