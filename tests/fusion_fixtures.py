"""DOM-fusion fixture data for agentpilot's suite.

Deliberately a copy of `packages/crawlpilot/tests/fusion_fixtures.py`. The two
suites must stand alone -- crawlpilot's runs in a clean venv with only
crawlpilot installed (CI job 1), so it cannot import from agentpilot's tests,
and agentpilot's runs against the *published* crawlpilot wheel (job 3), which
ships no test modules. Sharing one copy would couple two independently
installable projects through their test trees, which is exactly what the split
exists to prevent. Fixture data is cheap to duplicate; the coupling is not.
"""

from __future__ import annotations

from crawlpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from crawlpilot.spi.geometry import BoundingBox


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
