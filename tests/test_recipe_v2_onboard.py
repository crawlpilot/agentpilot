"""`agentpilot.recipe.v2.onboard` -- the exploration hook that turns an agent
run into a v2 document.

The browser, the agent loop and the selector agent are all faked: what is under
test is *when* a binding is frozen and *what steps travel with it*, which is
the whole of the recipe-building logic and none of the LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from fusion_fixtures import fnode

from agentpilot.agent.state import AgentStepRecord
from agentpilot.recipe.v2 import onboard as onboard_mod
from agentpilot.recipe.v2.models import Candidate, Locator
from agentpilot.recipe.v2.onboard import (
    ExplorationState,
    OnboardOutcome,
    derive_target,
    looks_variable,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.validate import validate_document

# `asyncio_mode = "auto"` (pyproject.toml) runs the async tests here; marking
# the module would also mark the sync ones, which pytest-asyncio warns about.


def _tree():
    return fnode("main", "Product", "e1", children=[
        fnode("button", "Details", "e5"),
        fnode("group", "Select size", "e10", children=[
            fnode("button", "S", "e11"),
            fnode("button", "M", "e12"),
            fnode("button", "L", "e13"),
        ]),
    ])


class _Reader:
    """Stands in for `PageReader`: a fixed tree, no page."""

    # Verification transforms the value it reads, and `url_resolve` needs the
    # page it was read from -- so the fake has to carry one too.
    base_url = "https://x.test/p/1"

    def __init__(self, url: str = "https://x.test/p/1") -> None:
        self.invalidated = 0
        self.url = url
        # What an earlier group's bindings read when re-checked. `None` means
        # "still there"; set it to simulate a step that closed what a previous
        # one opened.
        self.reads: object = "value"
        # What `PageReader.overlay` reports. No dialog, unless a test says so.
        self.dialog: dict[str, Any] = {
            "open": False, "locked": False, "close": None, "label": None,
        }

    async def overlay(self) -> dict[str, Any]:
        return self.dialog

    async def current_url(self) -> str:
        return self.url

    def invalidate(self) -> None:
        self.invalidated += 1

    async def snapshot(self):
        return _tree()

    async def structured_data(self) -> dict[str, Any]:
        return {"json_ld": [], "metadata": {}, "hydration": {}}

    async def read(self, locator):
        return self.reads


def _css(selector: str) -> list[Candidate]:
    return [Candidate(locator=Locator(kind="css", selector=selector), priority=60)]


def _step(actions: list[dict[str, Any]]) -> AgentStepRecord:
    return AgentStepRecord(
        step_number=1, evaluation_previous_goal="", memory="", next_goal="",
        actions=actions, action_results=[],
    )


@pytest.fixture
def patched(monkeypatch):
    """Route `propose_and_verify` and `propose_rows` at per-state stubs.

    `rows` defaults to finding nothing, which is what sends a table down the
    click-through path -- the behaviour every test written before `rows.py`
    assumed, so they keep testing what they were written to test.
    """

    async def fake(fields, **kwargs):
        # The stub lives on the state; the module-level patch just forwards.
        return fake.answer(dict(fields))  # type: ignore[attr-defined]

    async def fake_rows(spec, **kwargs):
        return fake.rows(spec)  # type: ignore[attr-defined]

    fake.rows = lambda spec: None
    monkeypatch.setattr(onboard_mod, "propose_and_verify", fake)
    monkeypatch.setattr(onboard_mod, "propose_rows", fake_rows)
    return fake


SCALARS = {
    "title": FieldSpec(name="title", type=TypeSpec(kind="scalar")),
    "price": FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="price")),
}

WITH_TABLE = {
    **SCALARS,
    "variants": FieldSpec(
        name="variants",
        type=TypeSpec(kind="table", columns={"size": TypeSpec(), "stock": TypeSpec()}),
    ),
}


async def test_a_reveal_belongs_to_the_field_it_revealed_not_to_global_setup(
    patched,
) -> None:
    """`global_setup` runs before EVERY group, so promoting a field-specific
    reveal into it makes every other group perform that reveal too -- and a
    drawer opened for one field is exactly the thing that covers the control
    another field needs.

    So the batch that satisfied a field keeps its own steps; only batches that
    bound nothing are groundwork."""

    patched.answer = lambda unfound: {"title": _css("h1")}
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    assert state.global_setup == []
    assert [s.op for s in state.field_groups[0].steps] == ["click", "wait_for_selector"]


async def test_groundwork_before_any_field_is_global_setup(patched) -> None:
    """The cookie banner, which is what `global_setup` exists for: dismissed in
    a batch that bound nothing, so it is not a reveal for anything and every
    group needs it."""

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    assert [s.op for s in state.global_setup] == ["click"]
    # ...and the group's own route starts after it, with the reveal that
    # actually satisfied the field.
    assert [s.op for s in state.field_groups[0].steps] == ["click", "wait_for_selector"]


async def test_a_group_carries_the_whole_route_from_a_cold_page(patched) -> None:
    """THE defect this round exists for. Replay re-navigates before every group
    and then runs that group's steps, so a group's steps have to stand alone
    from a freshly loaded page.

    Handing a group only the steps since the last freeze is what left the
    Walmart specifications group carrying `click "More details"` without the
    accordion click that puts it on screen -- so it read an empty page on every
    run and said nothing about why."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    # Batch 1 opens the accordion and satisfies the scalars.
    patched.answer = lambda unfound: {"title": _css("h1"), "price": _css("#p")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    # Batch 2 opens the dialog and satisfies the table's columns.
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    table = state.field_groups[-1]
    assert table.repeat is not None
    # Both clicks, in order -- not just the one from its own batch. The trailing
    # click is the representative option, superseded by the repeat.
    assert [s.op for s in table.steps] == ["click", "wait_for_selector"]
    assert table.steps[0].target is not None
    assert table.steps[0].target.name_contains == "Details"


async def test_reveals_on_one_page_share_one_group(patched) -> None:
    """Every group costs a page load: `replay.py` re-navigates before each one.
    Opening a second accordion on a page that is already open needs no reload,
    so it must not buy one."""

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    assert len(state.field_groups) == 1
    assert sorted(state.field_groups[0].bindings) == ["price", "title"]
    assert state.global_setup == []
    # One route covering both reveals, rather than two groups and two loads.
    assert [s.op for s in state.field_groups[0].steps] == [
        "click", "wait_for_selector", "click", "wait_for_selector",
    ]


async def test_a_navigation_forces_a_new_group(patched) -> None:
    """The earlier fields belong to a different page now. Merging would re-read
    them after the navigation and collect the wrong page's values -- a wrong
    answer that looks like a right one."""

    reader = _Reader()
    state = ExplorationState(fields=SCALARS, reader=reader, llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    reader.url = "https://x.test/p/2"
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    assert len(state.field_groups) == 2


async def test_a_step_that_closed_the_last_one_forces_a_new_group(patched) -> None:
    """Opening a second accordion usually leaves the first open; switching to a
    second TAB usually does not, and nothing about the step says which it was.
    So the previous group's bindings are re-read against the page as it stands,
    which is the only thing that actually answers it."""

    reader = _Reader()
    state = ExplorationState(fields=SCALARS, reader=reader, llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    # The earlier field no longer reads: the tab that held it was replaced.
    reader.reads = None
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    assert len(state.field_groups) == 2


async def test_a_group_with_a_repeat_is_never_merged_into(patched) -> None:
    """It clicks through an option set, so anything sharing its page load would
    be read against whichever option happened to be selected last."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([]))

    assert len(state.field_groups) == 2
    assert state.field_groups[0].repeat is not None


async def test_a_revealing_step_is_followed_by_a_wait(patched) -> None:
    """The driver returns from a click as soon as it is dispatched. Without an
    explicit wait the group reads the page as it was before the drawer opened
    -- and the engine has no implicit settle by design."""

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#details .price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    wait = state.field_groups[0].steps[-1]
    assert wait.op == "wait_for_selector"
    assert wait.optional is True


async def test_no_wait_is_added_after_a_non_revealing_step(patched) -> None:
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "WaitAction", "ms": 100}]))

    # No wait synthesised: `wait` is not a revealing op, so there is nothing
    # whose rendering has to be waited for.
    assert [s.op for s in state.field_groups[0].steps] == ["wait"]


async def test_a_table_field_gets_a_repeat_and_is_keyed_by_column(patched) -> None:
    """`validate_document`: a table carries the FIELD in `field_names` and its
    COLUMNS in `bindings`, and `repeat.row_field` must be one of the former."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}

    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    group = state.field_groups[0]
    assert group.field_names == ["variants"]
    assert sorted(group.bindings) == ["size", "stock"]
    assert group.repeat is not None
    assert group.repeat.row_field == "variants"
    assert group.repeat.option_locator.name_in == ["S", "M", "L"]


async def test_the_representative_click_is_not_also_a_reveal_step(patched) -> None:
    """It is superseded by the group's own RepeatSpec. Left in place the option
    would be clicked twice -- once to reveal, once as row one."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}

    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    assert state.global_setup == []
    assert state.field_groups[0].steps == []


async def test_a_dialog_left_open_is_closed_before_the_next_reveal(patched) -> None:
    """A modal opened to expose one field covers everything under it. The
    dismissal goes at the head of the NEXT batch, never the tail of this one:
    the fields just frozen are often inside the dialog, so appending it here
    would close the thing being read."""

    reader = _Reader()
    dispatched: list[Any] = []

    async def dispatch(step):
        dispatched.append(step)
        reader.dialog = {"open": False, "locked": False, "close": None, "label": None}

    state = ExplorationState(
        fields=SCALARS, reader=reader, llm_config=None,  # type: ignore[arg-type]
        dispatch_step=dispatch,
    )

    reader.dialog = {
        "open": True, "locked": True, "close": "#close", "label": "Close",
    }
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    # Dispatched, so the agent's remaining steps are not spent under an overlay.
    assert [s.op for s in dispatched] == ["click"]
    # ...and the group that read from inside the dialog kept its own bindings.
    assert state.field_groups[0].bindings == {"title": _css("h1")}

    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    # The dismissal leads the steps that follow, so the next reveal is not
    # clicking at a page under an overlay.
    steps = [s.op for s in state.field_groups[-1].steps]
    assert steps[0] == "click"
    assert state.field_groups[-1].steps[0].label is not None
    assert "close the dialog" in state.field_groups[-1].steps[0].label


async def test_no_dialog_means_no_step(patched) -> None:
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    labels = [s.label for group in state.field_groups for s in group.steps]
    assert not any(label and "close the dialog" in label for label in labels)


async def test_a_table_read_as_rows_keeps_the_click_that_revealed_it(patched) -> None:
    """The trailing click is stripped only for the click-through kind, where it
    is the representative-option click the RepeatSpec supersedes. A `dom_rows`
    table has no such click, so the trailing one is an ordinary reveal -- and
    stripping it deletes the very step that opened the drawer the rows are in."""

    from agentpilot.recipe.v2.models import RepeatSpec
    from agentpilot.recipe.v2.rows import RowBinding

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {}
    patched.rows = lambda spec: RowBinding(
        repeat=RepeatSpec(
            kind="dom_rows", row_field="variants", max_iterations=20,
            rows_locator=Locator(kind="css", selector="tr"),
        ),
        bindings={"size": _css("th"), "stock": _css("td")},
        rows=[{"size": "S", "stock": "in"}, {"size": "M", "stock": "out"}],
    )

    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    assert len(state.field_groups) == 1
    group = state.field_groups[0]
    assert group.repeat is not None and group.repeat.kind == "dom_rows"
    assert sorted(group.bindings) == ["size", "stock"]
    # The click became global_setup rather than being thrown away.
    assert [s.op for s in state.global_setup] == ["click"]
    # The columns are satisfied by the repeat; only the scalars nobody proposed
    # are still outstanding.
    assert sorted(state.unfound_fields) == ["price", "title"]


async def test_a_click_through_table_still_loses_its_representative_click(patched) -> None:
    """Unchanged, and the reason it must stay: the RepeatSpec clicks each option
    itself, so leaving the click as a reveal step clicks the option twice."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}

    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    assert state.global_setup == []
    group = state.field_groups[0]
    assert group.repeat is not None and group.repeat.kind == "dom"


async def test_a_table_bound_to_only_some_of_its_columns_is_not_frozen(patched) -> None:
    """Seen in a real Walmart build: `name` bound, `value` rejected by the
    scalar/list guard, and a `dom` repeat frozen that would emit `{name: ...}`
    rows for ever. A missing column then looks like the page not having a value
    rather than like the recipe never looking for one.

    Waiting costs nothing: the columns keep their place among the unfound, so a
    later step or a person can still satisfy them."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"size": _css(".size")}

    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    assert state.field_groups == []
    assert "variants" in state.failures
    assert "stock" in state.failures["variants"]
    assert sorted(state.unfound_fields) == ["price", "size", "stock", "title"]


async def test_a_table_whose_rows_cannot_be_iterated_stays_unresolved(patched) -> None:
    """`validate_document` rejects a table group with no repeat, so emitting one
    would produce a document that cannot be saved. It goes to the assist loop
    with a reason instead."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    # Both columns, so this reaches the repeat check rather than stopping at the
    # every-column guard above it.
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}

    # No click in the batch, so there is no representative option to generalise.
    await state.on_step(_step([{"type": "WaitAction", "ms": 10}]))

    assert state.field_groups == []
    # Reported under the TABLE, not under `size`. The columns are how a table
    # gets located; what the caller asked for was `variants`, and that is the
    # only name a person answering the ask can act on.
    assert "variants" in state.failures
    assert "size" not in state.failures
    assert "iterate" in state.failures["variants"]


async def test_unfound_fields_are_reported_with_a_reason(patched) -> None:
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"title": _css("h1")}

    await state.on_step(_step([]))

    assert list(state.unfound_fields) == ["price"]
    assert state.failures["price"]
    # A resolved field is not reported as failing.
    assert "title" not in state.failures


async def test_the_document_it_builds_passes_the_server_side_lint(patched) -> None:
    """The agent gets no exemption from the lint a human author is held to."""

    from agentpilot.recipe.v2.models import Recipe

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"title": _css("h1"), "price": _css("#p")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    recipe = Recipe(
        recipe_id="r", tenant="t", name="n", version=1,
        target=derive_target(["https://x.test/p/1"]),
        fields=WITH_TABLE,
        global_setup=state.global_setup,
        field_groups=state.field_groups,
    )
    errors, _warnings = validate_document(recipe.to_dict())
    assert errors == []


# --- target derivation ------------------------------------------------------


def test_a_single_url_is_cut_at_its_first_identifying_segment() -> None:
    """`productpage.129.html` carries the product number, so it identifies this
    page; `en_in` describes what kind of page it is."""

    target = derive_target(["https://www2.hm.com/en_in/productpage.129.html"])
    assert [(m.kind, m.pattern) for m in target.match] == [
        ("glob", "https://www2.hm.com/en_in/*")
    ]


def test_a_single_url_generalises_past_a_slug_and_not_just_the_last_segment() -> None:
    """THE bug this exists for. Keeping everything but the final segment turns a
    Walmart product URL into a matcher for that one product's sub-paths, and
    `replay_recipe` then refuses every other Walmart product before it opens a
    browser -- so a recipe sold as reusable cannot be run on a second page."""

    from agentpilot.recipe.v2.urlmatch import target_accepts

    target = derive_target([
        "https://www.walmart.com/ip/Bodycology-Moisturizing-Body-Cream-8-oz/5013580"
    ])
    assert [(m.kind, m.pattern) for m in target.match] == [
        ("glob", "https://www.walmart.com/ip/*")
    ]
    assert target_accepts(target, "https://www.walmart.com/ip/Something-Else/998877")
    # Generalised, not merely loosened: another site's /ip/ is still refused.
    assert not target_accepts(target, "https://evil.test/ip/anything/1")


def test_a_url_with_nothing_identifying_in_it_keeps_the_old_rule() -> None:
    """One sample cannot support a guess it has no evidence for, and being too
    narrow is visible and editable where being too wide is neither."""

    target = derive_target(["https://a.test/shop/all"])
    assert [(m.kind, m.pattern) for m in target.match] == [("glob", "https://a.test/shop/*")]


def test_what_counts_as_identifying_a_page() -> None:
    assert looks_variable("5013580")
    assert looks_variable("productpage.129.html")
    assert looks_variable("a-p041.html")
    assert looks_variable("Bodycology-Moisturizing-Body-Cream")
    assert looks_variable("9f8e7d6c5b4a3210")

    # Structural: these say what kind of page it is, not which one.
    assert not looks_variable("ip")
    assert not looks_variable("dp")
    assert not looks_variable("en_in")
    assert not looks_variable("products")
    assert not looks_variable("")


def test_several_urls_find_their_shared_shape() -> None:
    target = derive_target([
        "https://www.zara.com/in/en/a-p041.html",
        "https://www.zara.com/in/en/b-p092.html",
    ])
    assert [(m.kind, m.pattern) for m in target.match] == [
        ("glob", "https://www.zara.com/in/en/*")
    ]


def test_cross_host_samples_get_no_matcher_rather_than_a_universal_one() -> None:
    """`https://` is a common prefix of every URL on the internet. Trimming to
    it would emit the glob `https://*` -- a recipe that silently accepts
    everything, which is the exact thing `target.match` exists to prevent.
    `validate_document` already warns that an empty match accepts any URL."""

    from agentpilot.recipe.v2.urlmatch import target_accepts

    target = derive_target(["https://a.test/x", "https://b.test/y"])
    assert target.match == []

    universal = derive_target(["https://a.test/x"])
    assert not target_accepts(universal, "https://evil.test/anything")


def test_urls_diverging_at_the_path_root_fall_back_to_the_host() -> None:
    target = derive_target(["https://a.test", "https://a.test/x/y"])
    assert [(m.kind, m.pattern) for m in target.match] == [("host", "a.test")]


def test_no_samples_means_no_matcher() -> None:
    assert derive_target([]).match == []


def test_outcome_reports_completeness() -> None:
    assert OnboardOutcome().complete is True
    assert OnboardOutcome(unresolved={"price": "nope"}).complete is False


# --- assertions attached from the candidate chains --------------------------


async def test_a_field_with_two_kinds_of_locator_earns_a_cross_source_check(patched) -> None:
    """The chain the DOM-fallback pass produces is exactly what makes this
    check possible, and `replay.py` calls the second read "nearly free"."""

    from agentpilot.recipe.v2.assertions import baseline_assertions, with_assertions
    from agentpilot.recipe.v2.onboard import bindings_by_field

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {
        "title": [
            Candidate(locator=Locator(kind="hydration", path="p.title"), priority=15),
            Candidate(locator=Locator(kind="css", selector="h1"), priority=60),
        ],
        "price": _css("#price"),
    }
    await state.on_step(_step([]))

    chains = bindings_by_field(state.field_groups)
    checked = with_assertions(SCALARS, baseline_assertions(SCALARS, chains))

    assert [a.kind for a in checked["title"].assertions] == ["cross_source_agrees"]
    # `price` has one source, so there is nothing to compare it against -- but
    # it is still a price, and a negative one is always a parse gone wrong.
    assert [a.kind for a in checked["price"].assertions] == ["range"]


async def test_bindings_by_field_skips_a_tables_columns(patched) -> None:
    """A table's bindings are keyed by column, and its columns are not
    top-level fields -- so the table itself has no chain of its own."""

    from agentpilot.recipe.v2.onboard import bindings_by_field

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"size": _css(".size"), "stock": _css(".stock")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e12"}]))

    assert bindings_by_field(state.field_groups) == {}


# --- narrating a build that takes minutes ------------------------------------


async def test_progress_is_reported_after_every_step(patched) -> None:
    """A build drives a real browser for minutes. Without this the only thing
    the UI could show for that whole time was a spinner -- hiding exactly the
    evidence (a cookie wall, a consent dialog) a person could act on instantly."""

    seen: list[dict[str, Any]] = []

    async def sink(payload: dict[str, Any]) -> None:
        seen.append(payload)

    state = ExplorationState(
        fields=SCALARS, reader=_Reader(), llm_config=None, on_progress=sink,  # type: ignore[arg-type]
    )
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    assert len(seen) == 1
    assert seen[0]["phase"] == "exploring"
    assert seen[0]["found"] == ["title"]
    assert seen[0]["remaining"] == ["price"]
    assert seen[0]["steps"][0]["actions"] == ["ClickAction"]
    assert seen[0]["steps"][0]["found"] == ["title"]


async def test_a_step_that_found_nothing_still_reports(patched) -> None:
    """Otherwise the narration stalls on exactly the steps a watcher most wants
    to see -- the ones where it is stuck."""

    seen: list[dict[str, Any]] = []

    async def sink(payload: dict[str, Any]) -> None:
        seen.append(payload)

    state = ExplorationState(
        fields=SCALARS, reader=_Reader(), llm_config=None, on_progress=sink,  # type: ignore[arg-type]
    )
    patched.answer = lambda unfound: {}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    assert len(seen) == 1
    assert seen[0]["found"] == []
    assert seen[0]["steps"][0]["found"] == []


async def test_a_failing_progress_sink_cannot_break_the_build(patched) -> None:
    """A status line that can fail a build would be absurd."""

    async def boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("the database went away")

    state = ExplorationState(
        fields=SCALARS, reader=_Reader(), llm_config=None, on_progress=boom,  # type: ignore[arg-type]
    )
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    # The field was still frozen, which is the thing that actually matters.
    assert len(state.field_groups) == 1


# --- not looking for what is not there ---------------------------------------


async def test_a_field_that_keeps_missing_stops_being_proposed(patched) -> None:
    """THE loop, at its source. A field the page does not have was re-proposed
    on every exploration step -- fifteen model calls and fifteen identical
    failures for a value that was never there, because the contract could say
    "not found yet" but not "not there"."""

    asked: list[list[str]] = []

    def answer(unfound):
        asked.append(sorted(unfound))
        return {}

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = answer

    for _ in range(onboard_mod._MAX_FIELD_ATTEMPTS + 3):
        await state.on_step(_step([]))

    # Asked its budget of times, then it stopped asking. The budget itself is
    # read from the module rather than written here: it is a calibration
    # against real pages and has already been raised once, and a test that
    # hardcodes it fails for the wrong reason when it moves again.
    assert asked == [["price", "title"]] * onboard_mod._MAX_FIELD_ATTEMPTS
    assert state.presumed_absent == {"price", "title"}
    assert "may simply not be on this page" in state.failures["price"]


async def test_progress_anywhere_gives_every_field_its_patience_back(patched) -> None:
    """A step that found something is evidence the page moved somewhere useful:
    what was invisible a moment ago may be on screen now. Counting those against
    a field would give up on it for the crime of being behind an accordion --
    which is the entire reason the loop explores rather than reading once."""

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {}
    await state.on_step(_step([]))
    # `title` resolves; `price` misses, but the page demonstrably changed.
    patched.answer = lambda unfound: {"title": _css("h1")} if "title" in unfound else {}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {}
    await state.on_step(_step([]))

    # One miss before the progress, one after -- not two in a row.
    assert state.presumed_absent == set()


async def test_giving_up_on_one_field_does_not_stop_the_others(patched) -> None:
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {}
    for _ in range(onboard_mod._MAX_FIELD_ATTEMPTS):
        await state.on_step(_step([]))
    assert state.presumed_absent == {"price", "title"}

    # Nothing left worth asking about, so no proposal call is made at all.
    calls: list[int] = []

    def count(unfound):
        calls.append(len(unfound))
        return {}

    patched.answer = count
    await state.on_step(_step([]))
    assert calls == []
