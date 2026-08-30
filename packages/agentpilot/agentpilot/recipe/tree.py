"""Shared fused-tree walking helpers -- `stabilize.py`, `evaluate.py`,
`dispatch.py`, and `generalize.py` all need to find a node by ref, find its
parent, or find every node matching a role/name predicate, against the fused
`EnhancedDOMTreeNode` tree the perception engine builds.

Refs are `e<backendNodeId>` strings (`node_ref`); roles/names come from the
node's accessibility data (`ax_role` / `ax_name`)."""

from __future__ import annotations

from agentpilot.spi.dom_tree import EnhancedDOMTreeNode, NodeType


def node_ref(node: EnhancedDOMTreeNode) -> str:
    """The `e<backendNodeId>` ref string the serializer and ref cache use to
    address this node."""

    return f"e{node.backend_node_id}"


def _is_element(node: EnhancedDOMTreeNode) -> bool:
    return node.node_type == NodeType.ELEMENT_NODE


def find_node(node: EnhancedDOMTreeNode, ref: str) -> EnhancedDOMTreeNode | None:
    if _is_element(node) and node_ref(node) == ref:
        return node
    for child in node.children_and_shadow_roots:
        found = find_node(child, ref)
        if found is not None:
            return found
    return None


def find_parent(
    node: EnhancedDOMTreeNode, ref: str, parent: EnhancedDOMTreeNode | None = None
) -> EnhancedDOMTreeNode | None:
    if _is_element(node) and node_ref(node) == ref:
        return parent
    for child in node.children_and_shadow_roots:
        found = find_parent(child, ref, node)
        if found is not None:
            return found
    return None


def find_matches(
    node: EnhancedDOMTreeNode,
    role: str,
    name_contains: str | None = None,
    name_in: list[str] | None = None,
) -> list[EnhancedDOMTreeNode]:
    matches: list[EnhancedDOMTreeNode] = []
    if _is_element(node) and node.ax_role == role and node.ax_name:
        ok = True
        if name_contains and name_contains.lower() not in node.ax_name.lower():
            ok = False
        if name_in is not None and node.ax_name not in name_in:
            ok = False
        if ok:
            matches.append(node)
    for child in node.children_and_shadow_roots:
        matches.extend(find_matches(child, role, name_contains, name_in))
    return matches
