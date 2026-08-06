"""Unit tests for `agentpilot.agent.dom_diff` -- the headline change-detection.
Synthetic fused trees; no browser. Each of NEW/REMOVED/MOVED/MODIFIED plus the
cases the old `role:name`-path approach misclassified."""

from __future__ import annotations

from agentpilot.agent.dom_diff import ChangeKind, diff_snapshots, render_change_block
from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType


def _btn(
    backend: int,
    *,
    name: str = "",
    parent: EnhancedDOMTreeNode | None = None,
    role: str = "button",
    props: dict[str, str | bool] | None = None,
    tag: str = "BUTTON",
) -> EnhancedDOMTreeNode:
    node = EnhancedDOMTreeNode(
        node_id=backend,
        backend_node_id=backend,
        node_type=NodeType.ELEMENT_NODE,
        node_name=tag,
        attributes={},
        is_visible=True,
        ax_node=EnhancedAXNode(role=role, name=name, properties=props or {}),
    )
    if parent is not None:
        node.parent_node = parent
        parent.children_nodes.append(node)
    return node


def _page(*children_specs) -> EnhancedDOMTreeNode:
    """Build root>body and attach button specs; each spec is a kwargs dict."""
    root = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    for spec in children_specs:
        _btn(parent=root, **spec)
    return root


def test_first_observation_is_empty_diff() -> None:
    curr = _page({"backend": 10, "name": "Buy"})
    diff = diff_snapshots(None, curr)
    assert not diff.has_changes


def test_new_element_detected_and_marked() -> None:
    prev = _page({"backend": 10, "name": "Buy"})
    curr = _page({"backend": 10, "name": "Buy"}, {"backend": 11, "name": "Add coupon"})
    diff = diff_snapshots(prev, curr)
    new = diff.of_kind(ChangeKind.NEW)
    assert [c.node.backend_node_id for c in new] == [11]
    assert diff.new_backend_ids == {11}


def test_removed_element_detected() -> None:
    prev = _page({"backend": 10, "name": "Buy"}, {"backend": 11, "name": "Sign in"})
    curr = _page({"backend": 10, "name": "Buy"})
    diff = diff_snapshots(prev, curr)
    removed = diff.of_kind(ChangeKind.REMOVED)
    assert [c.node.backend_node_id for c in removed] == [11]


def test_modified_value_and_state() -> None:
    prev = _page(
        {"backend": 10, "name": "Email", "role": "textbox", "props": {"valuetext": ""}},
        {"backend": 11, "name": "Remember me", "role": "checkbox", "props": {"checked": False}},
    )
    curr = _page(
        {"backend": 10, "name": "Email", "role": "textbox", "props": {"valuetext": "a@b.com"}},
        {"backend": 11, "name": "Remember me", "role": "checkbox", "props": {"checked": True}},
    )
    diff = diff_snapshots(prev, curr)
    mods = diff.of_kind(ChangeKind.MODIFIED)
    ids = {c.node.backend_node_id for c in mods}
    assert ids == {10, 11}
    email = next(c for c in mods if c.node.backend_node_id == 10)
    assert "value:" in email.detail and "a@b.com" in email.detail


def test_moved_element_same_id_different_branch() -> None:
    # Same backend id, but re-parented (different ancestor tag chain).
    prev_root = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    header = _btn(2, parent=prev_root, tag="HEADER", role="banner", name="")
    _btn(10, parent=header, name="Buy")

    curr_root = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    footer = _btn(2, parent=curr_root, tag="FOOTER", role="contentinfo", name="")
    _btn(10, parent=footer, name="Buy")

    diff = diff_snapshots(prev_root, curr_root)
    moved = diff.of_kind(ChangeKind.MOVED)
    assert 10 in {c.node.backend_node_id for c in moved}
    assert not diff.of_kind(ChangeKind.NEW)  # not spuriously new


def test_ancestor_rename_does_not_spuriously_mark_new() -> None:
    """The failure mode of the old role:name-path identity: a change to an
    ancestor's name broke the path and marked unrelated children NEW. Here the
    button keeps its backend id, so it must be matched (MOVED at most), never
    NEW+REMOVED."""
    prev_root = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    section_a = _btn(2, parent=prev_root, tag="DIV", role="region", name="Step 1 of 3")
    _btn(10, parent=section_a, name="Next")

    curr_root = EnhancedDOMTreeNode(
        node_id=1, backend_node_id=1, node_type=NodeType.ELEMENT_NODE, node_name="BODY"
    )
    section_b = _btn(2, parent=curr_root, tag="DIV", role="region", name="Step 2 of 3")
    _btn(10, parent=section_b, name="Next")

    diff = diff_snapshots(prev_root, curr_root)
    assert not diff.of_kind(ChangeKind.NEW)
    assert not diff.of_kind(ChangeKind.REMOVED)


def test_backend_id_reassignment_absorbed_by_stable_hash() -> None:
    """A re-render can reassign backend ids. An element whose structure + attrs
    + name are unchanged must be matched via stable_hash, not reported NEW."""
    prev = _page({"backend": 10, "name": "Buy"})
    curr = _page({"backend": 99, "name": "Buy"})  # same element, new id
    diff = diff_snapshots(prev, curr)
    assert not diff.of_kind(ChangeKind.NEW)
    assert not diff.of_kind(ChangeKind.REMOVED)


def test_identical_siblings_do_not_collapse() -> None:
    """Two identical-looking buttons must both survive as distinct elements
    (the old path collapsed identical role:name identities into one)."""
    prev = _page({"backend": 10, "name": "Add"}, {"backend": 11, "name": "Add"})
    curr = _page(
        {"backend": 10, "name": "Add"},
        {"backend": 11, "name": "Add"},
        {"backend": 12, "name": "Add"},
    )
    diff = diff_snapshots(prev, curr)
    assert [c.node.backend_node_id for c in diff.of_kind(ChangeKind.NEW)] == [12]


def test_render_change_block_format() -> None:
    prev = _page({"backend": 10, "name": "Buy"})
    curr = _page({"backend": 10, "name": "Buy"}, {"backend": 11, "name": "Coupon"})
    block = render_change_block(diff_snapshots(prev, curr))
    assert block.startswith("## Changes since last step")
    assert 'NEW: [e11]<button "Coupon">' in block
    # Nothing changed -> empty string.
    assert render_change_block(diff_snapshots(curr, curr)) == ""
