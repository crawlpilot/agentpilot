"""Shared `SnapshotNode` tree-walking helpers -- `stabilize.py`, `evaluate.py`,
and `generalize.py` all need to find a node by ref, find its parent, or find
every node matching a role/name predicate, against the plain `AXSnapshot`
tree Phase 1 already builds (no DOM class/id/parent-selector info available,
just role/name/ref/children/bbox)."""

from __future__ import annotations

from agentpilot.spi.snapshot import SnapshotNode


def find_node(node: SnapshotNode, ref: str) -> SnapshotNode | None:
    if node.ref == ref:
        return node
    for child in node.children:
        found = find_node(child, ref)
        if found is not None:
            return found
    return None


def find_parent(
    node: SnapshotNode, ref: str, parent: SnapshotNode | None = None
) -> SnapshotNode | None:
    if node.ref == ref:
        return parent
    for child in node.children:
        found = find_parent(child, ref, node)
        if found is not None:
            return found
    return None


def find_matches(
    node: SnapshotNode,
    role: str,
    name_contains: str | None = None,
    name_in: list[str] | None = None,
) -> list[SnapshotNode]:
    matches: list[SnapshotNode] = []
    if node.ref and node.role == role and node.name:
        ok = True
        if name_contains and name_contains.lower() not in node.name.lower():
            ok = False
        if name_in is not None and node.name not in name_in:
            ok = False
        if ok:
            matches.append(node)
    for child in node.children:
        matches.extend(find_matches(child, role, name_contains, name_in))
    return matches
