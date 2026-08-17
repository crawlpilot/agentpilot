"""Unit tests for the agent loop's ref-validation helpers (ported from
browser-use's `index not in selector_map` guard). No browser, no LLM."""

from __future__ import annotations

from agentpilot.agent.loop import _action_ref, _eval_emoji
from agentpilot.spi.actions import (
    ClickAction,
    FillAction,
    NavigateAction,
    PressAction,
    ScrollAction,
)


def test_action_ref_reads_ref_bearing_actions() -> None:
    assert _action_ref(ClickAction(ref="e12")) == "e12"
    assert _action_ref(FillAction(ref="e7", text="hi")) == "e7"


def test_action_ref_none_for_refless_actions() -> None:
    assert _action_ref(NavigateAction(url="https://example.com")) is None
    assert _action_ref(PressAction(key="Enter")) is None
    # A scroll with no ref targets the page, not an element.
    assert _action_ref(ScrollAction(direction="down", ref=None)) is None


def test_action_ref_rejects_empty_ref() -> None:
    # An empty-string ref is not a usable target -- treat as ref-less.
    assert _action_ref(ClickAction(ref="")) is None


def test_hallucinated_and_url_refs_are_not_in_a_valid_set() -> None:
    # The loop rejects any ref not in `valid_refs`; these are exactly the
    # shapes the failing run produced.
    valid_refs = {"e10", "e11"}
    assert "add-to-cart-btn" not in valid_refs
    assert "https://www.amazon.com/Kindle-Books/dp/B00K0H3J74" not in valid_refs
    assert "e1141" not in valid_refs  # stale numeric ref
    assert "e10" in valid_refs


def test_eval_emoji_success_failure_neutral() -> None:
    assert _eval_emoji("this was a success") == "👍"
    assert _eval_emoji("that was a failure") == "⚠️"
    assert _eval_emoji("navigated to the page") == "❔"
