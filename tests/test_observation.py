"""Unit tests for `agentpilot.agent.observation.build_observation` -- the fusion
observation that fuses the change block with the serialized tree. No browser."""

from __future__ import annotations

from agentpilot.agent.observation import build_observation
from crawlpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from crawlpilot.spi.geometry import BoundingBox


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


def test_the_observation_leaves_out_text_the_agent_cannot_act_on() -> None:
    """An agent observation is a list of things to click, not a body of text to
    read, and on a commerce page the hidden half -- collapsed panels, offscreen
    carousel slides, SEO copy -- is most of the text by volume. It was crowding
    the controls out of a length-capped render entirely.

    `serialize` still renders hidden text by default; only this caller opts out.
    The recipe builder must keep seeing it, because a `text` locator is
    specified to read collapsed content and its judge corroborates against the
    same render.
    """

    body = _body(("Composition, care & origin", 10))
    panel = EnhancedDOMTreeNode(
        node_id=20, backend_node_id=20, node_type=NodeType.ELEMENT_NODE,
        node_name="DIV", is_visible=False,
    )
    blurb = EnhancedDOMTreeNode(
        node_id=21, backend_node_id=21, node_type=NodeType.TEXT_NODE,
        node_name="#text", node_value="100% viscose. Imported from China.",
        is_visible=False,
    )
    blurb.parent_node = panel
    panel.children_nodes.append(blurb)
    panel.parent_node = body
    body.children_nodes.append(panel)

    obs = build_observation(body)

    assert "100% viscose" not in obs.text
    # The control itself is untouched -- and still addressable.
    assert "[e10]" in obs.text
    assert "e10" in obs.visible_refs
