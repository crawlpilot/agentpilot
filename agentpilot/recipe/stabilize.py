"""Turns a dispatched, session-scoped action into a re-resolvable `RevealStep`.

`agentpilot.driver.ref_cache.RefCache` ties every `ref` to a specific
page/session epoch -- a `ClickAction(ref="e17")` recorded during exploration
is meaningless against a fresh page load in a later replay. Every action
that's captured into a recipe must instead reference the accessible
role+name the exploration snapshot already recorded for that ref, which
*does* generalize across page loads (barring a redesign, which is exactly
what `agentpilot.recipe.heal` re-runs exploration to fix).
"""

from __future__ import annotations

from typing import Any

from agentpilot.recipe.models import Locator, RevealStep
from agentpilot.recipe.tree import find_node
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.dom_tree import EnhancedDOMTreeNode

_DICT_ACTION_BUILDERS: dict[str, Any] = {
    "ClickAction": lambda d: spi_actions.ClickAction(ref=d["ref"]),
    "FillAction": lambda d: spi_actions.FillAction(ref=d["ref"], text=d["text"]),
    "HoverAction": lambda d: spi_actions.HoverAction(ref=d["ref"]),
    "SelectOptionAction": lambda d: spi_actions.SelectOptionAction(
        ref=d["ref"], values=d.get("values", [])
    ),
    "ScrollAction": lambda d: spi_actions.ScrollAction(direction=d["direction"], ref=d.get("ref")),
    "WaitAction": lambda d: spi_actions.WaitAction(ms=d.get("ms")),
    "PressAction": lambda d: spi_actions.PressAction(key=d["key"]),
}


def stabilize_action(action: spi_actions.Action, snapshot: EnhancedDOMTreeNode) -> RevealStep | None:
    """Returns `None` for actions that aren't reveal steps at all (navigate,
    go_back, extract, screenshot, tab management -- replay issues its own
    navigate and never needs the rest) or whose `ref` can't be resolved into
    a stable descriptor (not found in `snapshot`, or no accessible name to
    build an `ax_role` locator from -- rare for a genuinely interactive
    element)."""

    ref = getattr(action, "ref", None)
    locator: Locator | None = None
    if ref is not None:
        node = find_node(snapshot, ref)
        if node is None or not node.ax_name:
            return None
        locator = Locator(source="ax_role", role=node.ax_role, name_contains=node.ax_name)

    if isinstance(action, spi_actions.ClickAction):
        return RevealStep(action="click", locator=locator)
    if isinstance(action, spi_actions.FillAction):
        return RevealStep(action="fill", locator=locator, value=action.text)
    if isinstance(action, spi_actions.HoverAction):
        return RevealStep(action="hover", locator=locator)
    if isinstance(action, spi_actions.SelectOptionAction):
        return RevealStep(action="select_option", locator=locator, value=",".join(action.values))
    if isinstance(action, spi_actions.ScrollAction):
        return RevealStep(action="scroll", locator=locator)
    if isinstance(action, spi_actions.WaitAction):
        return RevealStep(action="wait", value=str(action.ms) if action.ms else None)
    if isinstance(action, spi_actions.PressAction):
        return RevealStep(action="press", value=action.key)
    return None


def stabilize_action_dict(
    action_dict: dict[str, Any], snapshot: EnhancedDOMTreeNode
) -> RevealStep | None:
    """`agent.state.AgentStepRecord.actions` stores dispatched actions as
    plain `{"type": <ClassName>, **fields}` dicts (`agent/loop.py`'s
    `_action_to_dict`), not the original dataclass instances -- this is the
    adapter `build.py`'s `on_step` hook uses, since that's all it receives.
    Returns `None` for an action type this module doesn't stabilize (same
    cases as `stabilize_action`) or a malformed/unrecognized dict."""

    builder = _DICT_ACTION_BUILDERS.get(action_dict.get("type", ""))
    if builder is None:
        return None
    try:
        action = builder(action_dict)
    except KeyError:
        return None
    return stabilize_action(action, snapshot)
