"""Deterministic replay end to end, with the browser stubbed.

The two behaviours that matter most here are the ones that cost something and
therefore look like mistakes until you know why: the per-group re-navigation,
and classifying the page before reading any field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from agentpilot.recipe.v2 import classify as classify_mod
from agentpilot.recipe.v2 import evaluate as ev
from agentpilot.recipe.v2 import replay as replay_mod
from agentpilot.recipe.v2 import steps as steps_mod
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Locator,
    Predicate,
    Recipe,
    RepeatSpec,
    RunInput,
    Step,
    TargetSpec,
    UrlMatcher,
)
from agentpilot.recipe.v2.replay import replay_recipe
from agentpilot.recipe.v2.schema import Assertion, FieldSpec, TypeSpec
from agentpilot.recipe.v2.transform import Transform
from agentpilot.recipe.v2.urlmatch import target_accepts

URL = "https://shop.test/p/1"
REAL_HTML = "<html><body>" + ("x" * 900) + "</body></html>"
WALL_HTML = "<html><body>Robot Check: enter the characters you see below</body></html>"


@dataclass
class FakeResult:
    extracts: list[str] = field(default_factory=list)
    js_returns: list[Any] = field(default_factory=list)
    fused_trees: list[Any] = field(default_factory=list)
    readouts: list[str] = field(default_factory=list)


class FakeBrowser:
    """Answers reads from canned data and records every dispatched action."""

    def __init__(self, *, structured=None, js=None, html=REAL_HTML) -> None:
        self.structured = structured or {"metadata": {}, "json_ld": [], "hydration": {}}
        self.js = js or {}
        self.html = html
        self.actions: list[str] = []
        self.navigations = 0

    async def execute(self, session, actions, *, registry, driver) -> FakeResult:
        out = FakeResult()
        for action in actions:
            name = type(action).__name__
            self.actions.append(name)
            if name == "NavigateAction":
                self.navigations += 1
            elif name == "ExtractAction":
                if getattr(action, "format", "") == "html":
                    out.extracts.append(self.html)
                else:
                    out.extracts.append(json.dumps(self.structured))
            elif name == "GetUrlAction":
                out.readouts.append(URL)
            elif name == "SnapshotAction":
                out.fused_trees = []
            elif name == "ExecuteJsAction":
                hit = next(
                    (v for k, v in self.js.items() if k in action.script), None
                )
                out.js_returns.append(hit)
        return out


@pytest.fixture
def browser(monkeypatch):
    def _make(**kw) -> FakeBrowser:
        fake = FakeBrowser(**kw)
        for mod in (ev, classify_mod, replay_mod, steps_mod):
            monkeypatch.setattr(mod, "execute_on_session", fake.execute, raising=False)
        return fake
    return _make


def recipe(**kw) -> Recipe:
    base = dict(
        recipe_id="r", tenant="t", name="n", version=1,
        target=TargetSpec(match=[UrlMatcher(kind="glob", pattern="https://shop.test/p/*")]),
        fields={"name": FieldSpec(name="name", type=TypeSpec(value_type="string"))},
        sample_urls=[URL],
        field_groups=[FieldGroup(
            group_id="g0", field_names=["name"],
            bindings={"name": [Candidate(locator=Locator(kind="json_ld", path="[0].name"))]},
        )],
    )
    base.update(kw)
    return Recipe(**base)  # type: ignore[arg-type]


async def run(r: Recipe, meta=None):
    return await replay_recipe(
        r, RunInput(url=URL, metadata=meta or {}),
        session=object(), registry=object(), driver=object(),
    )


# --- the target guard -------------------------------------------------------


def test_target_accepts_glob_host_and_regex() -> None:
    assert target_accepts(TargetSpec([UrlMatcher("glob", "https://shop.test/p/*")]), URL)
    assert target_accepts(TargetSpec([UrlMatcher("host", "shop.test")]), URL)
    assert target_accepts(TargetSpec([UrlMatcher("host", "*.shop.test")]),
                          "https://www.shop.test/x")
    assert target_accepts(TargetSpec([UrlMatcher("regex", r"/p/\d+$")]), URL)


def test_empty_match_accepts_anything() -> None:
    assert target_accepts(TargetSpec([]), "https://anywhere.test/")


def test_a_malformed_regex_declines_rather_than_raising() -> None:
    assert not target_accepts(TargetSpec([UrlMatcher("regex", "(unclosed")]), URL)


@pytest.mark.asyncio
async def test_a_non_matching_url_fails_before_a_browser_is_opened(browser) -> None:
    fake = browser()
    r = recipe(target=TargetSpec([UrlMatcher(kind="glob", pattern="https://other.test/*")]))
    res = await run(r)
    assert res.outcome == "failed"
    assert "does not match" in res.error
    assert fake.navigations == 0, "the cheapest possible failure is not navigating"


# --- blocked is not failed --------------------------------------------------


@pytest.mark.asyncio
async def test_a_bot_wall_is_blocked_not_failed(browser) -> None:
    """The whole reason classify.py exists: a challenge page has no fields,
    which is indistinguishable from every selector breaking."""

    browser(html=WALL_HTML)
    res = await run(recipe())
    assert res.outcome == "blocked"
    assert res.field_status == {}, "no field should be judged against a wall"


@pytest.mark.asyncio
async def test_an_almost_empty_page_is_also_treated_as_blocked(browser) -> None:
    browser(html="<html></html>")
    res = await run(recipe())
    assert res.outcome == "blocked"


@pytest.mark.asyncio
async def test_classification_happens_before_any_field_is_read(browser) -> None:
    fake = browser(html=WALL_HTML)
    await run(recipe())
    assert "ExecuteJsAction" not in fake.actions


# --- resolution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_run_is_ok_with_provenance(browser) -> None:
    browser(structured={"json_ld": [{"name": "Dove"}], "hydration": {}, "metadata": {}})
    res = await run(recipe())
    assert res.outcome == "ok"
    assert res.data["name"] == "Dove"
    assert res.field_status["name"] == "resolved"
    assert res.provenance["name"]["candidate"] == 0
    assert res.provenance["name"]["source"] == "json_ld"


@pytest.mark.asyncio
async def test_falling_back_to_a_later_candidate_is_recorded_and_downgrades_to_partial(
    browser,
) -> None:
    """Drift made visible: the recipe still works, but not the way it was
    built to."""

    browser(
        structured={"json_ld": [], "hydration": {}, "metadata": {"og:title": "Dove"}},
    )
    r = recipe(field_groups=[FieldGroup(
        group_id="g0", field_names=["name"],
        bindings={"name": [
            Candidate(locator=Locator(kind="json_ld", path="[0].name"), priority=10),
            Candidate(locator=Locator(kind="meta", path="og:title"), priority=20),
        ]},
    )])
    res = await run(r)
    assert res.data["name"] == "Dove"
    assert res.field_status["name"] == "fallback"
    assert res.provenance["name"]["candidate"] == 1


@pytest.mark.asyncio
async def test_a_required_field_that_resolves_nowhere_fails_the_run(browser) -> None:
    browser()
    r = recipe(fields={
        "name": FieldSpec(name="name", type=TypeSpec(value_type="string"), required=True)
    })
    res = await run(r)
    assert res.outcome == "failed"
    assert res.field_status["name"] == "failed"


@pytest.mark.asyncio
async def test_an_optional_missing_field_is_partial_not_failed(browser) -> None:
    browser()
    res = await run(recipe())
    assert res.outcome == "partial"
    assert res.field_status["name"] == "empty"


@pytest.mark.asyncio
async def test_a_declared_field_with_no_binding_cannot_be_reported_ok(browser) -> None:
    """A recipe that quietly stopped covering one of its declared fields must
    not pass."""

    browser(structured={"json_ld": [{"name": "Dove"}], "hydration": {}, "metadata": {}})
    r = recipe(fields={
        "name": FieldSpec(name="name", type=TypeSpec(value_type="string")),
        "never_bound": FieldSpec(name="never_bound", type=TypeSpec(value_type="string")),
    })
    res = await run(r)
    assert res.outcome == "partial"
    assert res.field_status["never_bound"] == "empty"


# --- assertions -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_assertion_marks_the_field_suspect_and_keeps_the_value(
    browser,
) -> None:
    browser(structured={"json_ld": [{"name": "x"}], "hydration": {}, "metadata": {}})
    r = recipe(fields={"name": FieldSpec(
        name="name", type=TypeSpec(value_type="string"),
        assertions=[Assertion(kind="length", min=5)],
    )})
    res = await run(r)
    assert res.data["name"] == "x", "flagging beats discarding"
    assert res.field_status["name"] == "suspect"
    assert res.outcome == "partial"
    assert res.assertions["name"][0].passed is False


@pytest.mark.asyncio
async def test_cross_source_disagreement_is_detected(browser) -> None:
    """The Walmart trap in miniature: two sources, two different numbers, and
    the winner looks perfectly well-typed on its own."""

    browser(structured={
        "json_ld": [{"price": "6.97"}], "hydration": {}, "metadata": {"og:price": "5.22"},
    })
    r = recipe(
        fields={"price": FieldSpec(
            name="price", type=TypeSpec(value_type="price"),
            assertions=[Assertion(kind="cross_source_agrees", tolerance=0.01)],
        )},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["price"],
            bindings={"price": [
                Candidate(locator=Locator(kind="json_ld", path="[0].price"), priority=10),
                Candidate(locator=Locator(kind="meta", path="og:price"), priority=20),
            ]},
        )],
    )
    res = await run(r)
    assert res.field_status["price"] == "suspect"
    assert "disagree" in res.assertions["price"][0].detail


# --- json repeats -----------------------------------------------------------


@pytest.mark.asyncio
async def test_json_repeat_builds_rows_without_a_single_click(browser) -> None:
    """The contract's central claim, executed: a whole variant table from one
    structured read."""

    fake = browser(structured={
        "json_ld": [{"hasVariant": [
            {"size": "XS", "offers": {"price": "9550", "availability": "OutOfStock"}},
            {"size": "S", "offers": {"price": "9550", "availability": "InStock"}},
        ]}],
        "hydration": {}, "metadata": {},
    })
    r = recipe(
        fields={"variants": FieldSpec(name="variants", type=TypeSpec(
            kind="table", columns={"size": TypeSpec(value_type="string")},
        ))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["variants"],
            bindings={"size": [Candidate(locator=Locator(kind="json_ld", path="size"))]},
            repeat=RepeatSpec(
                kind="json", row_field="variants", max_iterations=20,
                rows_locator=Locator(kind="json_ld", path="[0].hasVariant"),
            ),
        )],
    )
    res = await run(r)
    assert res.data["variants"] == [{"size": "XS"}, {"size": "S"}]
    assert res.truncated["variants"] is False
    assert "ClickAction" not in fake.actions


@pytest.mark.asyncio
async def test_hitting_max_iterations_is_reported_never_silent(browser) -> None:
    """A run that captured 8 of 40 sizes must not report success."""

    browser(structured={
        "json_ld": [{"hasVariant": [{"size": s} for s in "ABCDE"]}],
        "hydration": {}, "metadata": {},
    })
    r = recipe(
        fields={"variants": FieldSpec(name="variants", type=TypeSpec(
            kind="table", columns={"size": TypeSpec(value_type="string")}))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["variants"],
            bindings={"size": [Candidate(locator=Locator(kind="json_ld", path="size"))]},
            repeat=RepeatSpec(kind="json", row_field="variants", max_iterations=2,
                              rows_locator=Locator(kind="json_ld", path="[0].hasVariant")),
        )],
    )
    res = await run(r)
    assert len(res.data["variants"]) == 2
    assert res.truncated["variants"] is True
    assert res.field_status["variants"] == "suspect"
    assert res.outcome == "partial"


@pytest.mark.asyncio
async def test_expect_min_rows_flags_a_short_table(browser) -> None:
    browser(structured={
        "json_ld": [{"hasVariant": [{"size": "XS"}]}], "hydration": {}, "metadata": {},
    })
    from agentpilot.recipe.v2.models import Expectation

    r = recipe(
        fields={"variants": FieldSpec(name="variants", type=TypeSpec(
            kind="table", columns={"size": TypeSpec(value_type="string")}))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["variants"],
            bindings={"size": [Candidate(locator=Locator(kind="json_ld", path="size"))]},
            repeat=RepeatSpec(kind="json", row_field="variants", max_iterations=20,
                              rows_locator=Locator(kind="json_ld", path="[0].hasVariant")),
            expect=Expectation(min_rows=3),
        )],
    )
    res = await run(r)
    assert res.truncated["variants"] is True


# --- steps and isolation ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_run_shares_one_page_load(browser) -> None:
    """The point of the teardown contract, and worth pinning by count.

    This used to be one navigation per group: three groups meant four loads of
    the same URL to collect one record, which was the slowest part of a run and,
    on a protected site, the shape of a bot. What replaced it is the recipe
    owning the state it changes -- `steps` to reach it, `teardown` to put it
    back, and a guard on each reveal so a section an earlier group opened is left
    alone rather than toggled shut.
    """

    fake = browser(structured={"json_ld": [{"name": "x"}], "hydration": {}, "metadata": {}})
    groups = [
        FieldGroup(group_id=f"g{i}", field_names=["name"],
                   bindings={"name": [Candidate(locator=Locator(kind="json_ld",
                                                                path="[0].name"))]})
        for i in range(3)
    ]
    result = await run(recipe(field_groups=groups))
    assert fake.navigations == 1
    # And all three groups still read, on that one load.
    assert result.data["name"] == "x"


@pytest.mark.asyncio
async def test_a_skip_group_step_loses_only_that_group(browser) -> None:
    browser(structured={"json_ld": [{"name": "Dove"}], "hydration": {}, "metadata": {}})
    r = recipe(
        fields={
            "name": FieldSpec(name="name", type=TypeSpec(value_type="string")),
            "gated": FieldSpec(name="gated", type=TypeSpec(value_type="string")),
        },
        field_groups=[
            FieldGroup(group_id="ok", field_names=["name"],
                       bindings={"name": [Candidate(
                           locator=Locator(kind="json_ld", path="[0].name"))]}),
            FieldGroup(
                group_id="gated", field_names=["gated"],
                steps=[Step(op="select_option", label="always fails",
                            on_error="skip_group")],
                bindings={"gated": [Candidate(
                    locator=Locator(kind="json_ld", path="[0].name"))]},
            ),
        ],
    )
    res = await run(r)
    assert res.data["name"] == "Dove", "the healthy group still ran"
    assert res.field_status["gated"] == "failed"
    assert res.outcome == "partial"


@pytest.mark.asyncio
async def test_an_unsatisfied_guard_skips_its_step(browser) -> None:
    browser(
        structured={"json_ld": [{"name": "Dove"}], "hydration": {}, "metadata": {}},
        js={"querySelectorAll(opts.selector).length": 0},
    )
    r = recipe(global_setup=[Step(
        op="click", target=Locator(kind="css", selector="#consent"),
        when=[Predicate(kind="visible", selector="#consent")],
        label="dismiss consent",
    )])
    res = await run(r)
    skipped = [o for o in res.step_trace if o.status == "skipped"]
    assert skipped and skipped[0].label == "dismiss consent"
    assert res.outcome == "ok"


@pytest.mark.asyncio
async def test_metadata_reaches_a_step_argument(browser) -> None:
    fake = browser(structured={"json_ld": [{"name": "x"}], "hydration": {}, "metadata": {}})
    r = recipe(global_setup=[Step(op="find_text", args={"text": "{{meta.sku}}"})])
    await run(r, meta={"sku": "ABC123"})
    assert "FindTextAction" in fake.actions


# --- dom_rows: N row elements already on the page (v2.1) --------------------


@pytest.mark.asyncio
async def test_dom_rows_reads_every_row_in_one_pass(browser) -> None:
    """The commonest extraction there is, and the one v2.0 could not express.

    `json` needs the data to already be an array; `dom` clicks, which on a
    results page navigates away on the first row.
    """

    fake = browser(js={"opts.rows": {"rows": [
        {"title#0": "Alpha", "price#0": "10"},
        {"title#0": "Beta", "price#0": "20"},
        {"title#0": "Gamma", "price#0": "30"},
    ]}})
    r = recipe(
        fields={"items": FieldSpec(name="items", type=TypeSpec(
            kind="table",
            columns={"title": TypeSpec(value_type="string"),
                     "price": TypeSpec(value_type="string")},
        ))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["items"],
            bindings={
                "title": [Candidate(locator=Locator(kind="css", selector=".t"))],
                "price": [Candidate(locator=Locator(kind="css", selector=".p"))],
            },
            repeat=RepeatSpec(
                kind="dom_rows", row_field="items", max_iterations=50,
                rows_locator=Locator(kind="css", selector="li.card",
                                     within=Locator(kind="css", selector="#results")),
            ),
        )],
    )
    res = await run(r)
    assert res.data["items"] == [
        {"title": "Alpha", "price": "10"},
        {"title": "Beta", "price": "20"},
        {"title": "Gamma", "price": "30"},
    ]
    # One read for the whole matrix, and nothing was clicked.
    assert fake.actions.count("ExecuteJsAction") == 1
    assert "ClickAction" not in fake.actions


@pytest.mark.asyncio
async def test_dom_rows_keeps_rows_aligned_when_a_cell_is_missing(browser) -> None:
    """The bug this kind exists to prevent.

    Reading each column separately with `all: true` and zipping by index means
    a row that lacks a price silently takes the *next* row's price, and every
    value below shifts. Row-wise, a missing cell is a missing cell.
    """

    browser(js={"opts.rows": {"rows": [
        {"title#0": "Alpha", "price#0": "10"},
        {"title#0": "Beta", "price#0": None},
        {"title#0": "Gamma", "price#0": "30"},
    ]}})
    r = recipe(
        fields={"items": FieldSpec(name="items", type=TypeSpec(
            kind="table",
            columns={"title": TypeSpec(value_type="string"),
                     "price": TypeSpec(value_type="string")},
        ))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["items"],
            bindings={
                "title": [Candidate(locator=Locator(kind="css", selector=".t"))],
                "price": [Candidate(locator=Locator(kind="css", selector=".p"))],
            },
            repeat=RepeatSpec(kind="dom_rows", row_field="items", max_iterations=50,
                              rows_locator=Locator(kind="css", selector="li.card")),
        )],
    )
    res = await run(r)
    rows = res.data["items"]
    assert [row["title"] for row in rows] == ["Alpha", "Beta", "Gamma"]
    # Gamma keeps its own price rather than inheriting Beta's shifted one.
    assert rows[2]["price"] == "30"
    assert "price" not in rows[1]


@pytest.mark.asyncio
async def test_dom_rows_falls_through_a_columns_candidate_chain(browser) -> None:
    """A column's fallback chain has to work per row.

    Reading only each column's primary candidate would leave every fallback in
    the document and never try it -- the failure would look like an empty
    column on a recipe that plainly declares an alternative.
    """

    browser(js={"opts.rows": {"rows": [
        {"price#0": None, "price#1": "99"},
    ]}})
    r = recipe(
        fields={"items": FieldSpec(name="items", type=TypeSpec(
            kind="table", columns={"price": TypeSpec(value_type="string")}))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["items"],
            bindings={"price": [
                Candidate(priority=10, locator=Locator(kind="css", selector=".gone")),
                Candidate(priority=20, locator=Locator(kind="css", selector=".real")),
            ]},
            repeat=RepeatSpec(kind="dom_rows", row_field="items", max_iterations=50,
                              rows_locator=Locator(kind="css", selector="li")),
        )],
    )
    res = await run(r)
    assert res.data["items"] == [{"price": "99"}]


@pytest.mark.asyncio
async def test_dom_rows_reports_truncation(browser) -> None:
    browser(js={"opts.rows": {"rows": [{"t#0": s} for s in "ABCDE"]}})
    r = recipe(
        fields={"items": FieldSpec(name="items", type=TypeSpec(
            kind="table", columns={"t": TypeSpec(value_type="string")}))},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["items"],
            bindings={"t": [Candidate(locator=Locator(kind="css", selector=".t"))]},
            repeat=RepeatSpec(kind="dom_rows", row_field="items", max_iterations=2,
                              rows_locator=Locator(kind="css", selector="li")),
        )],
    )
    res = await run(r)
    assert len(res.data["items"]) == 2
    assert res.truncated["items"] is True
    assert res.field_status["items"] == "suspect"


@pytest.mark.asyncio
async def test_dom_rows_round_trips_through_the_wire_format() -> None:
    spec = RepeatSpec(
        kind="dom_rows", row_field="items", max_iterations=50,
        rows_locator=Locator(kind="css", selector="li.card"),
    )
    assert RepeatSpec.from_dict(spec.to_dict()) == spec
    assert spec.to_dict()["kind"] == "dom_rows"


@pytest.mark.asyncio
async def test_dom_rows_applies_the_field_transform_to_the_row_set(browser) -> None:
    """A specification table is a table on the page and a *map* in the answer.

    `to_object` is the contract's name for that collapse, and until the field's
    transform pipeline ran over the rows it was inert: a recipe could declare
    it, the studio could show it, and replay returned `[{name, value}, ...]`
    anyway. This is the shape the recipe studio emits for any `th`/`dt` spec
    table, so the two have to agree.
    """

    browser(js={"opts.rows": {"rows": [
        {"name#0": "Brand", "value#0": "Nike"},
        {"name#0": "Colour", "value#0": "Red"},
        {"name#0": "Material", "value#0": "Mesh"},
    ]}})
    r = recipe(
        fields={"specs": FieldSpec(
            name="specs",
            type=TypeSpec(kind="table", columns={
                "name": TypeSpec(value_type="string"),
                "value": TypeSpec(value_type="string"),
            }),
            transform=[Transform(op="to_object")],
        )},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["specs"],
            bindings={
                "name": [Candidate(locator=Locator(kind="css", selector=".k"))],
                "value": [Candidate(locator=Locator(kind="css", selector=".v"))],
            },
            repeat=RepeatSpec(kind="dom_rows", row_field="specs", max_iterations=50,
                              rows_locator=Locator(kind="css", selector="tr.row")),
        )],
    )
    res = await run(r)
    assert res.data["specs"] == {"Brand": "Nike", "Colour": "Red", "Material": "Mesh"}
    assert res.field_status["specs"] == "resolved"


@pytest.mark.asyncio
async def test_dom_rows_keeps_the_rows_when_the_reshape_fails(browser) -> None:
    """The read succeeded and only the reshaping did not.

    Dropping the field would lose data that was collected correctly, so the
    rows stand and the status says they are not what was asked for.
    """

    browser(js={"opts.rows": {"rows": [{"t#0": "Alpha"}, {"t#0": "Beta"}]}})
    r = recipe(
        fields={"items": FieldSpec(
            name="items",
            type=TypeSpec(kind="table", columns={"t": TypeSpec(value_type="string")}),
            # `join` with no `sep` is a transform that cannot be applied.
            transform=[Transform(op="join")],
        )},
        field_groups=[FieldGroup(
            group_id="g0", field_names=["items"],
            bindings={"t": [Candidate(locator=Locator(kind="css", selector=".t"))]},
            repeat=RepeatSpec(kind="dom_rows", row_field="items", max_iterations=50,
                              rows_locator=Locator(kind="css", selector="li")),
        )],
    )
    res = await run(r)
    assert res.data["items"] == [{"t": "Alpha"}, {"t": "Beta"}]
    assert res.field_status["items"] == "suspect"
