"""Pure, no-browser tests for the P1 "snapshot token budget" tree filters in
`agentpilot.driver.aria_parse`."""

from __future__ import annotations

from agentpilot.driver.aria_parse import (
    collect_leaf_refs,
    filter_snapshot,
    parse_aria_snapshot,
    prune_to_refs,
)

SNAPSHOT_TEXT = """- generic [ref=e1]:
  - heading "Title" [ref=e2]
  - link "A link" [ref=e3]
  - generic [ref=e4]:
    - button "Buy now" [ref=e5]
    - textbox "Search" [ref=e6]
"""


def _tree():
    return parse_aria_snapshot(SNAPSHOT_TEXT, epoch=1)


def test_roles_filter_keeps_matching_leaves_and_their_ancestors() -> None:
    root = filter_snapshot(_tree(), roles=("button",), max_nodes=None)
    refs = _collect_all_refs(root)
    assert refs == {"e1", "e4", "e5"}  # ancestors of the one button kept


def test_roles_filter_none_is_a_no_op() -> None:
    root = filter_snapshot(_tree(), roles=None, max_nodes=None)
    assert _collect_all_refs(root) == {"e1", "e2", "e3", "e4", "e5", "e6"}


def test_max_nodes_truncates_breadth_first() -> None:
    root = filter_snapshot(_tree(), roles=None, max_nodes=3)
    # Budget of 3 total nodes, breadth-first: the synthetic root (ref=""),
    # then e1 (its only child), then e2 (e1's first child) -- e3/e4 don't
    # fit, so their subtrees (including e5/e6) are dropped entirely.
    assert _collect_all_refs(root) == {"e1", "e2"}


def test_collect_leaf_refs_skips_containers() -> None:
    assert set(collect_leaf_refs(_tree())) == {"e2", "e3", "e5", "e6"}


def test_prune_to_refs_keeps_only_visible_leaves_and_ancestors() -> None:
    root = prune_to_refs(_tree(), visible={"e2", "e5"})
    assert _collect_all_refs(root) == {"e1", "e2", "e4", "e5"}


def _collect_all_refs(node) -> set[str]:
    refs = {node.ref} if node.ref else set()
    for child in node.children:
        refs |= _collect_all_refs(child)
    return refs
