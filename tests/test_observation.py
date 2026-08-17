"""Unit tests for `agentpilot.agent.observation.build_observation` -- the fusion
observation that fuses the change block with the serialized tree. No browser."""

from __future__ import annotations

from agentpilot.agent.observation import build_observation
from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from agentpilot.spi.geometry import BoundingBox


def _body(*names_ids: tuple[str, int]) -> EnhancedDOMTreeNode:
    body = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    for name, backend in names_ids:
        btn = EnhancedDOMTreeNode(
            node_id=backend,
            backend_node_id=backend,
            node_type=NodeType.ELEMENT_NODE,
            node_name="BUTTON",
            is_visible=True,
            absolute_position=BoundingBox(0, backend, 60, 20),
            ax_node=EnhancedAXNode(role="button", name=name),
        )
        btn.parent_node = body
        body.children_nodes.append(btn)
    return body


def test_first_observation_no_change_block() -> None:
    obs = build_observation(_body(("Buy", 10)))
    assert "Changes since last step" not in obs.text
    assert set(obs.selector_map) == {10}
    assert "[e10]" in obs.text
    assert not obs.diff.has_changes


def test_delta_observation_leads_with_change_block_and_marks_new() -> None:
    prev = _body(("Buy", 10))
    curr = _body(("Buy", 10), ("Coupon", 11))
    obs = build_observation(curr, prev)
    assert obs.text.startswith("## Changes since last step")
    assert 'NEW: [e11]<button "Coupon">' in obs.text
    assert "*[e11]" in obs.text  # inline new marker in the serialized tree
    assert set(obs.selector_map) == {10, 11}
    assert obs.diff.new_backend_ids == {11}
