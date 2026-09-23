"""`PageReader`: locator reads, predicate guards and variant detection.

The session is stubbed, so what is under test is the dispatch logic and the
caching -- not Chrome. The generated JS is asserted structurally (that an
xpath locator actually reaches `document.evaluate`, that options are passed as
JSON rather than interpolated) because those are the properties that would
otherwise only fail against a live page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from fusion_fixtures import fnode

from agentpilot.recipe.v2 import evaluate as ev
from agentpilot.recipe.v2.evaluate import LocatorError, PageReader, detect_variant
from agentpilot.recipe.v2.models import Locator, PageVariant, Predicate

WALMART = {
    "json_ld": [{"name": "Dove", "offers": {"price": "9550"}}],
    "hydration": {"props": {"idml": {"specifications": [
        {"name": "Scent", "value": "Strawberry Cookie"},
        {"name": "Form", "value": "Liquid"},
    ]}}},
    "metadata": {"og:title": "Dove Body Wash"},
}


@dataclass
class FakeResult:
    extracts: list[str] = field(default_factory=list)
    js_returns: list[Any] = field(default_factory=list)
    fused_trees: list[Any] = field(default_factory=list)


class FakeSession:
    """Records every dispatched action and answers from canned data."""

    def __init__(self, *, structured=None, js=None, tree=None) -> None:
        self.structured = structured if structured is not None else WALMART
        self.js = js or {}
        self.tree = tree
        self.scripts: list[str] = []
        self.extract_calls = 0
        self.snapshot_calls = 0

    async def execute(self, session, actions, *, registry, driver) -> FakeResult:
        action = actions[0]
        name = type(action).__name__
        if name == "ExtractAction":
            self.extract_calls += 1
            return FakeResult(extracts=[json.dumps(self.structured)])
        if name == "SnapshotAction":
            self.snapshot_calls += 1
            return FakeResult(fused_trees=[self.tree] if self.tree is not None else [])
        if name == "ExecuteJsAction":
            self.scripts.append(action.script)
            for needle, value in self.js.items():
                if needle in action.script:
                    return FakeResult(js_returns=[value])
            return FakeResult(js_returns=[None])
        return FakeResult()


@pytest.fixture
def reader(monkeypatch):
    def _make(**kw) -> tuple[PageReader, FakeSession]:
        fake = FakeSession(**kw)
        monkeypatch.setattr(ev, "execute_on_session", fake.execute)
        r = PageReader(session=object(), registry=object(), driver=object(),
                       base_url="https://www.walmart.com/ip/x/123")
        return r, fake
    return _make


# --- structured sources -----------------------------------------------------


@pytest.mark.asyncio
async def test_reads_json_ld_by_simple_path(reader) -> None:
    r, _ = reader()
    assert await r.read(Locator(kind="json_ld", path="[0].name")) == "Dove"


@pytest.mark.asyncio
async def test_reads_meta_from_the_metadata_container(reader) -> None:
    """The container is named `metadata` while the locator kind is `meta` --
    the one place the two vocabularies differ."""

    r, _ = reader()
    assert await r.read(Locator(kind="meta", path="og:title")) == "Dove Body Wash"


@pytest.mark.asyncio
async def test_reads_hydration_with_a_jmespath_filter(reader) -> None:
    r, _ = reader()
    loc = Locator(
        kind="hydration", path_lang="jmespath",
        path="props.idml.specifications[?name=='Scent'].value | [0]",
    )
    assert await r.read(loc) == "Strawberry Cookie"


@pytest.mark.asyncio
async def test_a_malformed_path_raises_rather_than_returning_none(reader) -> None:
    """A path that is invalid is an authoring bug; a path that matches nothing
    is an empty field. Collapsing the two hides the former forever."""

    r, _ = reader()
    with pytest.raises(LocatorError):
        await r.read(Locator(kind="hydration", path_lang="jmespath", path="a[?b=="))


@pytest.mark.asyncio
async def test_missing_path_is_none_not_an_error(reader) -> None:
    r, _ = reader()
    assert await r.read(Locator(kind="json_ld", path="[0].nope")) is None


@pytest.mark.asyncio
async def test_structured_data_is_fetched_once_and_shared(reader) -> None:
    """Walmart's blob is 352 KB. A group reading twelve fields out of it must
    cost one extraction, not twelve."""

    r, fake = reader()
    for _ in range(5):
        await r.read(Locator(kind="json_ld", path="[0].name"))
    assert fake.extract_calls == 1


@pytest.mark.asyncio
async def test_invalidate_forces_a_refetch(reader) -> None:
    """Holding a snapshot across a click is how a recipe reads the state it was
    trying to change."""

    r, fake = reader()
    await r.read(Locator(kind="json_ld", path="[0].name"))
    r.invalidate()
    await r.read(Locator(kind="json_ld", path="[0].name"))
    assert fake.extract_calls == 2


@pytest.mark.asyncio
async def test_absent_structured_data_yields_empty_containers(reader) -> None:
    """Amazon ships zero JSON-LD scripts; reading one must be empty, not a
    crash."""

    r, _ = reader(structured={"metadata": {}, "json_ld": [], "hydration": {}})
    assert await r.read(Locator(kind="json_ld", path="[0].name")) is None


# --- css / xpath ------------------------------------------------------------


@pytest.mark.asyncio
async def test_css_read_returns_the_value(reader) -> None:
    r, _ = reader(js={"querySelectorAll": {"value": "Dove"}})
    assert await r.read(Locator(kind="css", selector="h1")) == "Dove"


@pytest.mark.asyncio
async def test_xpath_reaches_document_evaluate(reader) -> None:
    """The capability that does not exist anywhere else in the stack."""

    r, fake = reader(js={"document.evaluate": {"value": "6.88 ounces"}})
    loc = Locator(kind="xpath", selector="//tr[th[normalize-space()='Item Weight']]/td")
    assert await r.read(loc) == "6.88 ounces"
    assert "document.evaluate" in fake.scripts[0]
    assert "XPathResult.ORDERED_NODE_SNAPSHOT_TYPE" in fake.scripts[0]


@pytest.mark.asyncio
async def test_selector_is_passed_as_json_not_interpolated(reader) -> None:
    """A selector containing a quote must be data, not syntax."""

    r, fake = reader(js={"querySelectorAll": {"value": "x"}})
    await r.read(Locator(kind="css", selector="[data-qa='it\"s']"))
    opts_line = fake.scripts[0]
    assert json.dumps("[data-qa='it\"s']") in opts_line


@pytest.mark.asyncio
async def test_options_carry_index_all_attribute_and_within(reader) -> None:
    r, fake = reader(js={"querySelectorAll": {"value": []}})
    await r.read(Locator(
        kind="css", selector="li", attribute="href", all=True, index=2,
        within=Locator(kind="css", selector=".drawer"),
    ))
    payload = json.loads(fake.scripts[0].split("const opts = ", 1)[1].split(";\n", 1)[0])
    assert payload == {
        "kind": "css", "selector": "li", "attribute": "href", "all": True, "index": 2,
        "within": {"kind": "css", "selector": ".drawer"},
        # An ordinary read does not pay for the common-ancestor walk. Replay
        # reads every field of every row this way, and it wants the value, not a
        # description of where the value lives.
        "want_scope": False,
    }


@pytest.mark.asyncio
async def test_reading_with_scope_asks_for_it_and_hands_it_back(reader) -> None:
    """The build asks where a locator's matches live; replay does not. One round
    trip, so the scope always describes the value that came back with it."""

    scope = {"spans_document": False, "tag": "section", "selector": "#specs", "matched": 5}
    r, fake = reader(js={"querySelectorAll": {"value": ["a", "b"], "scope": scope}})
    value, got = await r.read_with_scope(Locator(kind="css", selector="li", all=True))

    assert value == ["a", "b"]
    assert got == scope
    payload = json.loads(fake.scripts[0].split("const opts = ", 1)[1].split(";\n", 1)[0])
    assert payload["want_scope"] is True


@pytest.mark.asyncio
async def test_a_structured_locator_has_no_scope_to_report(reader) -> None:
    """A JSON path has no DOM container, so the question does not apply -- and
    answering it with something would invite a `within` that cannot resolve."""

    r, _fake = reader(structured={"json_ld": [{"name": "Dove"}]})
    value, scope = await r.read_with_scope(Locator(kind="json_ld", path="[0].name"))
    assert value == "Dove"
    assert scope is None


@pytest.mark.asyncio
async def test_text_reads_exclude_script_and_style_subtrees(reader) -> None:
    """Raw textContent includes the SOURCE of any inline <script> inside the
    element. Verified against the live page: Amazon's #availability contains a
    P.when(...) block, and its 'Customer Reviews' spec row carries an inline
    click handler -- both were returned as the field's value before this.

    innerText excludes them for free but also excludes collapsed content, which
    is exactly what `text` exists to reach, so the reader walks the subtree
    itself. The behaviour is asserted structurally here and end-to-end against
    the real page.
    """

    r, fake = reader(js={"querySelectorAll": {"value": "x"}})
    await r.read(Locator(kind="css", selector="#availability"))
    script = fake.scripts[0]
    assert "SCRIPT: 1" in script and "STYLE: 1" in script
    assert "textOf(el)" in script
    assert "el.textContent" not in script


@pytest.mark.asyncio
async def test_an_invalid_selector_is_reported_as_a_locator_error(reader) -> None:
    r, _ = reader(js={"querySelectorAll": {"error": "invalid selector"}})
    with pytest.raises(LocatorError, match="invalid selector"):
        await r.read(Locator(kind="css", selector="[["))


@pytest.mark.asyncio
async def test_unknown_locator_kind_is_an_error(reader) -> None:
    r, _ = reader()
    with pytest.raises(LocatorError, match="unknown locator kind"):
        await r.read(Locator(kind="telepathy"))  # type: ignore[arg-type]


# --- ax_role via the fused tree ---------------------------------------------


@pytest.mark.asyncio
async def test_ax_role_reads_through_the_snapshot(reader) -> None:
    tree = fnode(children=[fnode("button", "PRODUCT MEASUREMENTS", "e1")])
    r, _ = reader(tree=tree)
    loc = Locator(kind="ax_role", role="button", name_contains="MEASUREMENT")
    assert await r.read(loc) == "PRODUCT MEASUREMENTS"


@pytest.mark.asyncio
async def test_no_snapshot_yields_none_rather_than_raising(reader) -> None:
    r, _ = reader(tree=None)
    assert await r.read(Locator(kind="ax_role", role="button")) is None


# --- predicates -------------------------------------------------------------


@pytest.mark.asyncio
async def test_selector_present_and_absent_are_inverses(reader) -> None:
    r, _ = reader(js={"querySelectorAll(opts.selector).length": 3})
    assert await r.holds(Predicate(kind="selector_present", selector=".x")) is True
    assert await r.holds(Predicate(kind="selector_absent", selector=".x")) is False


@pytest.mark.asyncio
async def test_count_at_least(reader) -> None:
    r, _ = reader(js={"querySelectorAll(opts.selector).length": 3})
    assert await r.holds(Predicate(kind="count_at_least", selector=".c", n=3)) is True
    assert await r.holds(Predicate(kind="count_at_least", selector=".c", n=4)) is False


@pytest.mark.asyncio
async def test_json_path_present_detects_the_hydration_variant(reader) -> None:
    """How the Walmart recipe tells a hydrated page from a DOM-only one."""

    r, _ = reader()
    yes = Predicate(kind="json_path_present", source="hydration", path="props.idml")
    no = Predicate(kind="json_path_present", source="hydration", path="props.absent")
    assert await r.holds(yes) is True
    assert await r.holds(no) is False


@pytest.mark.asyncio
async def test_meta_equals_reads_the_run_metadata(reader) -> None:
    r, _ = reader()
    p = Predicate(kind="meta_equals", key="region", value="in")
    assert await r.holds(p, meta={"region": "in"}) is True
    assert await r.holds(p, meta={"region": "us"}) is False


@pytest.mark.asyncio
async def test_url_matches_against_the_run_url(reader) -> None:
    r, _ = reader()
    assert await r.holds(Predicate(kind="url_matches", url="*/ip/*")) is True
    assert await r.holds(Predicate(kind="url_matches", url="*/dp/*")) is False


@pytest.mark.asyncio
async def test_a_predicate_that_explodes_is_false_not_an_exception(reader) -> None:
    """A guard that explodes is worse than a guard that declines."""

    async def boom(*a, **k):
        raise RuntimeError("driver gone")

    r, fake = reader()
    r._eval_js = boom  # type: ignore[method-assign]
    assert await r.holds(Predicate(kind="selector_present", selector=".x")) is False


@pytest.mark.asyncio
async def test_unknown_predicate_kind_is_false(reader) -> None:
    r, _ = reader()
    assert await r.holds(Predicate(kind="from_the_future")) is False  # type: ignore[arg-type]


# --- variant detection ------------------------------------------------------


@pytest.mark.asyncio
async def test_first_satisfied_variant_by_priority_wins(reader) -> None:
    r, _ = reader()
    variants = [
        PageVariant(variant_id="dom_only", priority=20, detect=[
            Predicate(kind="json_path_present", source="hydration", path="props.absent")]),
        PageVariant(variant_id="hydrated", priority=10, detect=[
            Predicate(kind="json_path_present", source="hydration", path="props.idml")]),
    ]
    assert await detect_variant(variants, r) == "hydrated"


@pytest.mark.asyncio
async def test_every_predicate_in_a_variant_must_hold(reader) -> None:
    r, _ = reader()
    variants = [PageVariant(variant_id="v", detect=[
        Predicate(kind="json_path_present", source="hydration", path="props.idml"),
        Predicate(kind="json_path_present", source="hydration", path="props.absent"),
    ])]
    assert await detect_variant(variants, r) is None


@pytest.mark.asyncio
async def test_no_variant_matching_is_none_which_is_degraded_not_failed(reader) -> None:
    r, _ = reader()
    variants = [PageVariant(variant_id="v", detect=[
        Predicate(kind="selector_present", selector=".nope")])]
    assert await detect_variant(variants, r) is None


@pytest.mark.asyncio
async def test_no_variants_declared_is_none(reader) -> None:
    r, _ = reader()
    assert await detect_variant([], r) is None
