"""Pure unit tests for `agentpilot.recipe.stabilize` -- turning a
session-scoped dispatched action into a re-resolvable `RevealStep`, given a
static `AXSnapshot` fixture. No browser, no epoch/ref system involved."""

from __future__ import annotations

from agentpilot.recipe.stabilize import stabilize_action, stabilize_action_dict
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.snapshot import AXSnapshot, SnapshotNode


def _node(role: str, name: str = "", ref: str = "", children=None) -> SnapshotNode:
    return SnapshotNode(epoch=1, ref=ref, role=role, name=name, children=children or [])


SNAPSHOT = AXSnapshot(
    epoch=1,
    root=_node(
        "root",
        children=[
            _node("button", "Accept cookies", ref="e1"),
            _node("button", "", ref="e2"),  # no accessible name -- can't stabilize
        ],
    ),
)


def test_click_action_stabilizes_to_an_ax_role_locator() -> None:
    step = stabilize_action(spi_actions.ClickAction(ref="e1"), SNAPSHOT)
    assert step is not None
    assert step.action == "click"
    assert step.locator is not None
    assert step.locator.source == "ax_role"
    assert step.locator.role == "button"
    assert step.locator.name_contains == "Accept cookies"


def test_fill_action_carries_its_text_as_the_step_value() -> None:
    step = stabilize_action(spi_actions.FillAction(ref="e1", text="hello"), SNAPSHOT)
    assert step is not None
    assert step.action == "fill"
    assert step.value == "hello"


def test_click_on_a_ref_with_no_accessible_name_cannot_be_stabilized() -> None:
    assert stabilize_action(spi_actions.ClickAction(ref="e2"), SNAPSHOT) is None


def test_click_on_an_unresolvable_ref_returns_none() -> None:
    assert stabilize_action(spi_actions.ClickAction(ref="does-not-exist"), SNAPSHOT) is None


def test_navigate_action_is_never_a_reveal_step() -> None:
    assert stabilize_action(spi_actions.NavigateAction(url="https://x.test"), SNAPSHOT) is None


def test_extract_action_is_never_a_reveal_step() -> None:
    assert stabilize_action(spi_actions.ExtractAction(), SNAPSHOT) is None


def test_wait_action_has_no_locator_but_still_stabilizes() -> None:
    step = stabilize_action(spi_actions.WaitAction(ms=250), SNAPSHOT)
    assert step is not None
    assert step.locator is None
    assert step.value == "250"


def test_stabilize_action_dict_matches_the_dataclass_path() -> None:
    action_dict = {"type": "ClickAction", "ref": "e1", "all": False, "terminates_sequence": False}
    step = stabilize_action_dict(action_dict, SNAPSHOT)
    assert step == stabilize_action(spi_actions.ClickAction(ref="e1"), SNAPSHOT)


def test_stabilize_action_dict_unknown_type_returns_none() -> None:
    action_dict = {"type": "NavigateAction", "url": "https://x.test"}
    assert stabilize_action_dict(action_dict, SNAPSHOT) is None


def test_stabilize_action_dict_malformed_dict_returns_none() -> None:
    assert stabilize_action_dict({"type": "ClickAction"}, SNAPSHOT) is None
