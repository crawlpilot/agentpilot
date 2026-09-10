"""`agentpilot.recipe.v2.capture` -- turning what the agent did into what a
recipe replays. Pure: a hand-built fused tree, no browser.

Two things here are regression guards rather than API tests. The
dispatchability checks pin failures that `validate_document` cannot see and
that therefore surface only as a recipe which fails on every single run. The
wait-synthesis test pins a policy that is implemented twice, in two languages.
"""

from __future__ import annotations

import re
from pathlib import Path

from fusion_fixtures import fnode

from agentpilot.recipe.v2 import capture
from agentpilot.recipe.v2.models import Candidate, Locator, RepeatSpec
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode

_FROMPICK = (
    Path(__file__).resolve().parents[1]
    / "frontend" / "src" / "lib" / "recipe" / "fromPick.ts"
)


def _option_tree() -> EnhancedDOMTreeNode:
    """A size picker: three same-role options under one named parent."""

    return fnode("main", "Product", "e1", children=[
        fnode("group", "Select size", "e10", children=[
            fnode("button", "S", "e11"),
            fnode("button", "M", "e12"),
            fnode("button", "L", "e13"),
        ]),
    ])


# --- dispatchability -------------------------------------------------------


def test_an_xpath_action_target_is_refused_at_capture_time() -> None:
    """`steps.py` raises on an xpath action target: the driver resolves
    selectors with querySelector, not an XPath engine. A recipe carrying one
    passes `validate_document` and then fails on every run, so the capture is
    the bug."""

    reason = capture.dispatchability_error(
        Locator(kind="xpath", selector="//button[text()='M']"), "click"
    )
    assert reason is not None
    assert "xpath" in reason


def test_an_indexed_css_action_target_is_refused_at_capture_time() -> None:
    """`steps.py` refuses rather than silently acting on the first match --
    which would make a dom repeat click the same option on every iteration and
    return N identical rows, a wrong answer that looks like a right one."""

    reason = capture.dispatchability_error(
        Locator(kind="css", selector=".size", index=2), "click"
    )
    assert reason is not None
    assert "index=2" in reason


def test_a_css_target_scoped_by_a_non_css_within_is_refused() -> None:
    """`_compose_css` raises for this rather than dropping the scope, because
    silently widening a scope is how the v1 false positive happened."""

    reason = capture.dispatchability_error(
        Locator(
            kind="css",
            selector=".price",
            within=Locator(kind="ax_role", role="group", name_contains="Details"),
        ),
        "click",
    )
    assert reason is not None


def test_ordinary_targets_are_accepted() -> None:
    assert capture.dispatchability_error(None, "wait") is None
    assert capture.dispatchability_error(Locator(kind="css", selector="#go"), "click") is None
    assert (
        capture.dispatchability_error(
            Locator(kind="ax_role", role="button", name_contains="M"), "click"
        )
        is None
    )


# --- stabilising -----------------------------------------------------------


def test_a_click_becomes_an_ax_role_step_not_a_ref() -> None:
    """A ref is tied to one page/session epoch, so a recipe that stored one
    would address nothing on the next page load."""

    tree = _option_tree()
    step = capture.stabilize_action_dict({"type": "ClickAction", "ref": "e12"}, tree)

    assert step is not None
    assert step.op == "click"
    assert step.target is not None
    assert step.target.kind == "ax_role"
    assert (step.target.role, step.target.name_contains) == ("button", "M")


def test_a_fill_carries_its_text_into_args() -> None:
    tree = _option_tree()
    step = capture.stabilize_action_dict(
        {"type": "FillAction", "ref": "e12", "text": "hello"}, tree
    )
    assert step is not None
    assert step.op == "fill"
    assert step.args == {"text": "hello"}


def test_an_unresolvable_ref_is_dropped_rather_than_guessed() -> None:
    tree = _option_tree()
    assert capture.stabilize_action_dict({"type": "ClickAction", "ref": "e999"}, tree) is None


def test_a_node_with_no_accessible_name_is_dropped() -> None:
    """There is nothing stable to describe it by, and an ax_role locator with
    no name would match the first button on the page."""

    tree = fnode("main", "Product", "e1", children=[fnode("button", "", "e11")])
    assert capture.stabilize_action_dict({"type": "ClickAction", "ref": "e11"}, tree) is None


def test_navigation_and_friends_are_not_reveal_steps() -> None:
    """Replay issues its own navigate; capturing one would make the group
    navigate twice."""

    tree = _option_tree()
    for action in ({"type": "NavigateAction", "url": "x"}, {"type": "ScreenshotAction"}):
        assert capture.stabilize_action_dict(action, tree) is None


def test_last_click_ref_finds_the_trailing_click() -> None:
    actions = [
        {"type": "ClickAction", "ref": "e11"},
        {"type": "ClickAction", "ref": "e12"},
        {"type": "WaitAction", "ms": 100},
    ]
    assert capture.last_click_ref(actions) == "e12"
    assert capture.last_click_ref([{"type": "WaitAction"}]) is None


# --- generalising ----------------------------------------------------------


def test_one_clicked_option_generalises_to_the_whole_set() -> None:
    tree = _option_tree()
    repeat = capture.generalize_option_locator(
        snapshot=tree, clicked_ref="e12", row_field="variants", max_iterations=20
    )

    assert repeat is not None
    assert repeat.kind == "dom"
    assert repeat.row_field == "variants"
    assert repeat.option_locator is not None
    assert repeat.option_locator.name_in == ["S", "M", "L"]


def test_the_option_set_is_scoped_to_its_parent() -> None:
    """v1 matched `name_in` against the whole tree and called the resulting
    false positive an accepted risk. It is not hypothetical: a size guide
    rendering XS/S/M in a drawer while the page behind it renders the same
    labels. `within` is why v2 does not have to accept it."""

    tree = _option_tree()
    repeat = capture.generalize_option_locator(
        snapshot=tree, clicked_ref="e12", row_field="variants", max_iterations=20
    )

    assert repeat is not None
    within = repeat.option_locator.within  # type: ignore[union-attr]
    assert within is not None
    assert (within.role, within.name_contains) == ("group", "Select size")


def test_a_lone_option_falls_back_to_a_single_iteration() -> None:
    """Better a table with one row than a table field that cannot resolve at
    all -- a later heal can improve on it."""

    tree = fnode("main", "p", "e1", children=[
        fnode("group", "g", "e10", children=[fnode("button", "M", "e11")]),
    ])
    assert (
        capture.generalize_option_locator(
            snapshot=tree, clicked_ref="e11", row_field="variants", max_iterations=20
        )
        is None
    )
    fallback = capture.single_option_fallback(
        snapshot=tree, clicked_ref="e11", row_field="variants"
    )
    assert fallback is not None
    assert fallback.max_iterations == 1
    assert fallback.option_locator.name_in == ["M"]  # type: ignore[union-attr]


# --- the wait, and the cross-language contract -----------------------------


def test_a_wait_is_synthesised_for_the_group_container() -> None:
    repeat = RepeatSpec(
        kind="dom_rows",
        row_field="items",
        max_iterations=20,
        rows_locator=Locator(
            kind="css", selector="li.card", within=Locator(kind="css", selector="ul.results")
        ),
    )
    step = capture.wait_step_for(candidates=[], repeat=repeat)

    assert step is not None
    assert step.op == "wait_for_selector"
    # The container, not a row: a drawer can render its list element before it
    # has any children.
    assert step.target.selector == "ul.results"  # type: ignore[union-attr]
    assert step.args == {"state": "visible"}
    assert step.timeout_ms == capture.REVEAL_TIMEOUT_MS
    # A reveal that never renders is an empty field, not a failed run.
    assert step.optional is True
    assert step.effective_on_error == "continue"


def test_a_scalar_wait_composes_its_within_into_the_selector() -> None:
    candidates = [
        Candidate(
            locator=Locator(
                kind="css", selector=".price", within=Locator(kind="css", selector="#details")
            )
        )
    ]
    step = capture.wait_step_for(candidates=candidates, repeat=None)
    assert step is not None
    assert step.target.selector == "#details .price"  # type: ignore[union-attr]


def test_no_wait_when_there_is_nothing_css_to_wait_on() -> None:
    """A field read purely out of JSON has no element to become visible, and a
    wait on nothing would just burn the timeout."""

    candidates = [Candidate(locator=Locator(kind="hydration", path="props.price"))]
    assert capture.wait_step_for(candidates=candidates, repeat=None) is None


def test_the_revealing_op_set_matches_the_frontends() -> None:
    """The same policy, implemented twice in two languages.

    Both halves author recipes into the same contract, so an op that only one
    of them thinks needs a wait is a race that reproduces on one authoring path
    and not the other -- and the engine has no implicit settle to cover for it
    by design. This is the second such duplication in the codebase; the first
    (`paths.py` / `probe.ts`) silently blanked every JSON-bound field until it
    was pinned the same way.
    """

    source = _FROMPICK.read_text()
    block = re.search(
        r"const REVEALING_OPS = new Set<StepOp>\(\[(.*?)\]\)", source, re.S
    )
    assert block is not None, "REVEALING_OPS not found in fromPick.ts"
    frontend = set(re.findall(r"'([a-z_]+)'", block.group(1)))

    assert frontend == set(capture.REVEALING_OPS)


def test_the_reveal_timeout_matches_the_frontends() -> None:
    source = _FROMPICK.read_text()
    found = re.search(r"const REVEAL_TIMEOUT_MS = ([0-9_]+)", source)
    assert found is not None
    assert int(found.group(1).replace("_", "")) == capture.REVEAL_TIMEOUT_MS


# --- closing what a reveal left open -----------------------------------------


def _overlay(**over):
    base = {"open": False, "locked": False, "close": None, "label": None}
    base.update(over)
    return base


def test_a_dialog_with_a_close_control_is_closed_by_clicking_it() -> None:
    step = capture.dismiss_step_for(
        _overlay(open=True, close='button[aria-label="Close"]', label="Close")
    )
    assert step is not None
    assert step.op == "click"
    assert step.target is not None
    assert step.target.selector == 'button[aria-label="Close"]'
    # A dialog that did not appear this time is not a failed run -- the rule
    # every reveal step in this system already follows.
    assert step.optional is True
    assert step.on_error == "continue"


def test_a_dialog_with_no_close_control_gets_escape() -> None:
    """Not a guess for its own sake: a great many dialogs answer Escape, and the
    alternative here is no step at all -- which leaves the overlay covering
    every later click and scroll."""

    step = capture.dismiss_step_for(_overlay(open=True))
    assert step is not None
    assert step.op == "press"
    assert step.args == {"key": "Escape"}
    assert step.optional is True


def test_a_scroll_lock_with_no_visible_dialog_still_gets_a_dismissal() -> None:
    """The lock is the thing that breaks a later `scroll` step, and it outlives
    a dialog that has faded its overlay out without removing it."""

    assert capture.dismiss_step_for(_overlay(locked=True)) is not None


def test_a_clear_page_gets_no_step() -> None:
    assert capture.dismiss_step_for(_overlay()) is None
