"""The getters return values, not sentences.

Until 0.2 every one of these returned `ActionResult.readouts[0]` -- prose
written for an agent's prompt. `get_count()` gave
`"count: 3 element(s) match '.item'"` instead of `3`, and `is_visible()` gave
`"#x is visible"` or `"#x is not visible"`: two non-empty strings, so
`if await page.is_visible(x)` was **always true** and every caller checking
visibility had a silent bug.

These tests pin the types. `test_is_visible_is_falsy_when_not_visible` is the
one that matters -- it is the regression that motivated the version bump.

No browser: a recording driver returns the `ActionResult` a real one would.
"""

from __future__ import annotations

from typing import Any

import pytest

from crawlpilot.api import Browser
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.actions import ActionResult
from crawlpilot.spi.driver import ContextRef

from .test_api_facade import RecordingDriver


class ValueDriver(RecordingDriver):
    """`RecordingDriver`, but answering query actions with canned values.

    Subclassed rather than rewritten so the context/lease plumbing -- which is
    what makes `Browser.session()` work at all -- stays in one place.
    """

    def __init__(self, values: dict[type, Any]) -> None:
        super().__init__()
        self._values = values

    async def execute(
        self, ctx: ContextRef, actions: list[spi_actions.Action], page_id: str | None = None
    ) -> ActionResult:
        self.batches.append(actions)
        result = ActionResult()
        for action in actions:
            if type(action) in self._values:
                value = self._values[type(action)]
                result.values.append(value)
                # A real driver always produces both; the prose is what the
                # agent path reads and must keep existing.
                result.readouts.append(f"prose for {type(action).__name__}: {value!r}")
        return result


def _session(values: dict[type, Any], tmp_path: Any) -> Browser:
    return Browser(driver=ValueDriver(values), profiles_root=tmp_path / "p")


@pytest.mark.parametrize(
    ("action_cls", "value", "call", "expected", "expected_type"),
    [
        (spi_actions.GetTitleAction, "Widget Shop", "get_title", "Widget Shop", str),
        (spi_actions.GetUrlAction, "https://x.test/a", "get_url", "https://x.test/a", str),
        (spi_actions.GetCountAction, 3, "get_count", 3, int),
        (spi_actions.IsVisibleAction, True, "is_visible", True, bool),
        (spi_actions.IsEnabledAction, False, "is_enabled", False, bool),
        (spi_actions.GetTextAction, "hello", "get_text", "hello", str),
        (spi_actions.GetValueAction, "typed", "get_value", "typed", str),
        (spi_actions.GetStylesAction, {"color": "red"}, "get_styles", {"color": "red"}, dict),
        (spi_actions.SearchPageAction, ["a", "b"], "search_page", ["a", "b"], list),
    ],
)
async def test_getters_return_real_values(
    tmp_path: Any,
    action_cls: type,
    value: Any,
    call: str,
    expected: Any,
    expected_type: type,
) -> None:
    browser = _session({action_cls: value}, tmp_path)
    async with browser:
        async with browser.session() as page:
            method = getattr(page, call)
            got = await (method(".item") if call in {"get_count", "search_page"} else method())

    assert got == expected
    assert isinstance(got, expected_type), f"{call} returned {type(got).__name__}"


async def test_is_visible_is_falsy_when_not_visible(tmp_path: Any) -> None:
    """The bug this release exists for.

    `"#x is not visible"` is a non-empty string, so the natural way to write
    the check -- `if await page.is_visible(...)` -- took the *visible* branch
    for an invisible element, in every caller's code, silently.
    """

    browser = _session({spi_actions.IsVisibleAction: False}, tmp_path)
    async with browser:
        async with browser.session() as page:
            visible = await page.is_visible(selector="#hidden")

    assert visible is False
    assert not visible, "an invisible element must be falsy, not a truthy sentence"


async def test_is_checked_distinguishes_indeterminate_from_unchecked(tmp_path: Any) -> None:
    """A tri-state checkbox is `None`, not `False`: neither answer is true of
    `indeterminate`, and `False` would report an unanswered box as deliberately
    unchecked."""

    browser = _session({spi_actions.IsCheckedAction: None}, tmp_path)
    async with browser:
        async with browser.session() as page:
            assert await page.is_checked(selector="#tri") is None

    browser = _session({spi_actions.IsCheckedAction: False}, tmp_path)
    async with browser:
        async with browser.session() as page:
            assert await page.is_checked(selector="#plain") is False


async def test_get_attribute_keeps_absent_distinct_from_empty(tmp_path: Any) -> None:
    """`None` (no such attribute) and `""` (`<input required="">`) are different
    answers, so the getter does not flatten one into the other."""

    browser = _session({spi_actions.GetAttributeAction: None}, tmp_path)
    async with browser:
        async with browser.session() as page:
            assert await page.get_attribute("data-x", selector="#a") is None

    browser = _session({spi_actions.GetAttributeAction: ""}, tmp_path)
    async with browser:
        async with browser.session() as page:
            assert await page.get_attribute("required", selector="#a") == ""


async def test_the_agent_prose_still_exists_alongside_the_value(tmp_path: Any) -> None:
    """`values` is an addition, not a replacement.

    The agent loop and `tools/catalog.py` read `readouts`, and they must keep
    reading exactly what they read before -- the two channels answer different
    questions for different consumers.
    """

    browser = _session({spi_actions.GetTitleAction: "Widget Shop"}, tmp_path)
    async with browser:
        async with browser.session() as page:
            result = await page.execute([spi_actions.GetTitleAction()])

    assert result.values == ["Widget Shop"]
    assert result.readouts and isinstance(result.readouts[0], str)
    assert len(result.values) == len(result.readouts), "the two lists are index-correlated"
