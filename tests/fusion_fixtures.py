"""Builders for synthetic fused `EnhancedDOMTreeNode` trees used across the
recipe/agent unit tests (no browser). Mirrors the small role/name/ref trees the
tests previously expressed as `AXSnapshot`, in the single fusion representation:
a node is addressable as `e<backendNodeId>` and carries its accessibility
`role`/`name` as `ax_node` data.
"""

from __future__ import annotations

from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from agentpilot.spi.geometry import BoundingBox


def fnode(
    role: str = "",
    name: str = "",
    ref: str = "",
    *,
    tag: str | None = None,
    children: list[EnhancedDOMTreeNode] | None = None,
    bbox: BoundingBox | None = None,
    visible: bool = True,
) -> EnhancedDOMTreeNode:
    """A fused element node addressable as `ref` (an `e<backendNodeId>` string),
    carrying `role`/`name` as its accessibility data. `ref=""` -> backend id 0
    (an unaddressed container/root). `tag` defaults to the role so role-based
    interactivity detection classifies e.g. a `button` node as interactive."""

    backend = int(ref[1:]) if ref.startswith("e") and ref[1:].isdigit() else 0
    node = EnhancedDOMTreeNode(
        node_id=backend,
        backend_node_id=backend,
        node_type=NodeType.ELEMENT_NODE,
        node_name=(tag or role or "div").upper(),
        is_visible=visible,
        absolute_position=bbox,
        ax_node=EnhancedAXNode(role=role or None, name=name or None),
        children_nodes=list(children or []),
    )
    for child in node.children_nodes:
        child.parent_node = node
    return node
