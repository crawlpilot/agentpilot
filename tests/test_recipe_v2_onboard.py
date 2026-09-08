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
from agentpilot.recipe.v2.onboard import ExplorationState, OnboardOutcome, derive_target
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

    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1

    async def snapshot(self):
        return _tree()

    async def structured_data(self) -> dict[str, Any]:
        return {"json_ld": [], "metadata": {}, "hydration": {}}

    async def read(self, locator):
        return "value"


def _css(selector: str) -> list[Candidate]:
    return [Candidate(locator=Locator(kind="css", selector=selector), priority=60)]


def _step(actions: list[dict[str, Any]]) -> AgentStepRecord:
    return AgentStepRecord(
        step_number=1, evaluation_previous_goal="", memory="", next_goal="",
        actions=actions, action_results=[],
    )


@pytest.fixture
def patched(monkeypatch):
    """Route `propose_and_verify` at a per-state stub."""

    async def fake(fields, **kwargs):
        # The stub lives on the state; the module-level patch just forwards.
        return fake.answer(dict(fields))  # type: ignore[attr-defined]

    monkeypatch.setattr(onboard_mod, "propose_and_verify", fake)
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


async def test_steps_before_the_first_field_become_global_setup(patched) -> None:
    """Replay re-navigates before every group, so anything needed to make the
    *first* field readable is needed for all of them."""

    patched.answer = lambda unfound: {"title": _css("h1")}
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    assert [s.op for s in state.global_setup] == ["click"]
    # ...and the group that was satisfied by it does not repeat it.
    assert state.field_groups[0].steps == []


async def test_later_steps_are_scoped_to_their_own_group(patched) -> None:
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]

    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e11"}]))

    assert len(state.field_groups) == 2
    assert [s.op for s in state.global_setup] == ["click"]
    # The second group carries its own click, plus the synthesised wait.
    assert [s.op for s in state.field_groups[1].steps] == ["click", "wait_for_selector"]


async def test_a_revealing_step_is_followed_by_a_wait(patched) -> None:
    """The driver returns from a click as soon as it is dispatched. Without an
    explicit wait the group reads the page as it was before the drawer opened
    -- and the engine has no implicit settle by design."""

    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#details .price")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))

    wait = state.field_groups[1].steps[-1]
    assert wait.op == "wait_for_selector"
    assert wait.target.selector == "#details .price"
    assert wait.optional is True


async def test_no_wait_is_added_after_a_non_revealing_step(patched) -> None:
    state = ExplorationState(fields=SCALARS, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"title": _css("h1")}
    await state.on_step(_step([{"type": "ClickAction", "ref": "e5"}]))
    patched.answer = lambda unfound: {"price": _css("#price")}
    await state.on_step(_step([{"type": "WaitAction", "ms": 100}]))

    assert [s.op for s in state.field_groups[1].steps] == ["wait"]


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


async def test_a_table_whose_rows_cannot_be_iterated_stays_unresolved(patched) -> None:
    """`validate_document` rejects a table group with no repeat, so emitting one
    would produce a document that cannot be saved. It goes to the assist loop
    with a reason instead."""

    state = ExplorationState(fields=WITH_TABLE, reader=_Reader(), llm_config=None)  # type: ignore[arg-type]
    patched.answer = lambda unfound: {"size": _css(".size")}

    # No click in the batch, so there is no representative option to generalise.
    await state.on_step(_step([{"type": "WaitAction", "ms": 10}]))

    assert state.field_groups == []
    assert "size" in state.failures
    assert "iterate" in state.failures["size"]


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


def test_a_single_url_keeps_everything_but_the_last_segment() -> None:
    target = derive_target(["https://www2.hm.com/en_in/productpage.129.html"])
    assert [(m.kind, m.pattern) for m in target.match] == [
        ("glob", "https://www2.hm.com/en_in/*")
    ]


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
