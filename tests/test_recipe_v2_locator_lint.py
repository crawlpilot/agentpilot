"""An xpath expression must stay inside the thing it is scoped to.

Written against the locator a real Zara build produced and froze:

    //*[contains(translate(text(),'CARE','care'),'care')]/following::ul[1]/li

It collected "Bag0", "LOG IN", "Help" and two nav blocks alongside the five
washing instructions it was aimed at. Both of its defects are here.
"""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2.locator_lint import (
    partial_case_fold_reason,
    relativize_xpath,
    scoped_xpath,
    xpath_escape_reason,
)

CARE = "//*[contains(translate(text(),'CARE','care'),'care')]/following::ul[1]/li"


def test_the_locator_that_collected_the_site_header_is_refused() -> None:
    reason = xpath_escape_reason(CARE, scoped=False)
    assert reason is not None
    assert "following::" in reason
    # The reason is fed back to the model verbatim, so it has to say what is
    # wrong rather than that something is.
    assert "leaves any container" in reason


@pytest.mark.parametrize(
    "axis",
    ["following::ul", "preceding::div", "ancestor::section", "ancestor-or-self::div"],
)
def test_every_document_order_axis_is_refused(axis: str) -> None:
    """None of these can be contained by a context node, so no `within` beside
    them would mean anything."""

    assert xpath_escape_reason(f".//p/{axis}", scoped=False) is not None


@pytest.mark.parametrize(
    "selector",
    [
        ".//tr[th[normalize-space()='Item Weight']]/following-sibling::td",
        ".//dt[normalize-space()='Brand']/preceding-sibling::dd",
    ],
)
def test_the_sibling_axes_survive(selector: str) -> None:
    """Bounded by the parent, and the one thing CSS cannot express -- selecting
    a cell by its sibling's text, which the prompts teach xpath for. Banning the
    whole `following` family would have taken these with it."""

    assert xpath_escape_reason(selector, scoped=False) is None


def test_an_absolute_path_is_only_wrong_once_something_should_contain_it() -> None:
    """On an unscoped locator `//` is the ordinary way to write a document
    query, and refusing it would refuse most correct xpath."""

    assert xpath_escape_reason("//ul/li", scoped=False) is None

    reason = xpath_escape_reason("//ul/li", scoped=True)
    assert reason is not None
    assert ".//ul/li" in reason  # says what to write instead


def test_relativizing_only_touches_the_anchor() -> None:
    assert relativize_xpath("//ul/li") == ".//ul/li"
    assert relativize_xpath("/html/body/ul") == "./html/body/ul"
    # Already relative: nothing to do, and saying so lets the caller tell
    # "unchanged" from "rewritten".
    assert relativize_xpath(".//ul/li") is None
    assert relativize_xpath("") is None
    # `scoped_xpath` folds the no-op away for callers that just want a string.
    assert scoped_xpath(".//ul/li") == ".//ul/li"
    assert scoped_xpath("//ul/li") == ".//ul/li"


# --- the case fold ----------------------------------------------------------


def test_a_four_letter_case_fold_is_reported() -> None:
    """`translate(text(),'CARE','care')` lower-cases exactly C, A, R and E, so
    it matches "Customer care" and "Careful" as readily as the heading it was
    aimed at."""

    reason = partial_case_fold_reason(CARE)
    assert reason is not None
    assert "only those 4 letters" in reason
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in reason  # says what to write instead


def test_the_full_alphabet_fold_is_accepted() -> None:
    good = (
        ".//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'care')]"
    )
    assert partial_case_fold_reason(good) is None


def test_an_expression_with_no_translate_says_nothing() -> None:
    assert partial_case_fold_reason(".//li") is None
    assert partial_case_fold_reason("") is None


def test_the_case_fold_is_advice_not_a_refusal() -> None:
    """A short `translate()` can be deliberate. It is reported as a warning so a
    correct-but-unusual expression is not thrown away -- unlike an escaping
    axis, which is never salvageable."""

    good_axis = ".//*[contains(translate(., 'CARE', 'care'), 'care')]"
    assert xpath_escape_reason(good_axis, scoped=False) is None
    assert partial_case_fold_reason(good_axis) is not None
