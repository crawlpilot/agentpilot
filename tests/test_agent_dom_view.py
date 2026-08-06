"""Pure unit tests for `agentpilot.agent.dom_view` -- static `AXSnapshot`
fixtures, zero browser."""

from __future__ import annotations

from agentpilot.agent.dom_view import render_snapshot_for_llm
from agentpilot.spi.snapshot import AXSnapshot, BoundingBox, SnapshotNode


def _node(role: str, name: str = "", ref: str = "", children=None, bbox=None) -> SnapshotNode:
    return SnapshotNode(
        epoch=1, ref=ref, role=role, name=name, children=children or [], bbox=bbox
    )


def test_interactive_node_rendered_with_ref_bracket() -> None:
    root = _node("root", children=[_node("button", "Submit", ref="e1")])
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root))
    assert "[e1]<button" in text
    assert "Submit" in text


def test_non_interactive_named_node_rendered_without_brackets() -> None:
    root = _node("root", children=[_node("heading", "Welcome", ref="e2")])
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root))
    assert "heading" in text
    assert "[e2]" not in text


def test_node_with_no_name_and_no_interactive_role_is_omitted() -> None:
    root = _node("root", children=[_node("generic", ref="e3")])
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root))
    assert "e3" not in text


def test_bbox_included_when_present() -> None:
    root = _node(
        "root",
        children=[_node("button", "Go", ref="e4", bbox=BoundingBox(x=10, y=20, width=5, height=5))],
    )
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root))
    assert "@(10,20)" in text


def test_new_node_marked_with_star_prefix_relative_to_previous() -> None:
    previous_root = _node("root", children=[_node("button", "Old", ref="e1")])
    previous = AXSnapshot(epoch=1, root=previous_root)

    current_root = _node(
        "root",
        children=[_node("button", "Old", ref="e1"), _node("button", "New", ref="e5")],
    )
    current = AXSnapshot(epoch=2, root=current_root)

    text = render_snapshot_for_llm(current, previous)
    lines = text.splitlines()
    old_line = next(line for line in lines if "Old" in line)
    new_line = next(line for line in lines if "New" in line)
    assert not old_line.strip().startswith("*")
    assert new_line.strip().startswith("*[e5]")


def test_inserted_element_shifts_refs_but_only_new_one_is_marked() -> None:
    # Regression for the ephemeral-ref diff: inserting an element at the front
    # shifts Playwright's traversal-order refs (Old moves e1 -> e2). A ref-set
    # diff would mark the shifted "Old" as new; the identity diff must mark
    # only the genuinely-new "Inserted".
    previous = AXSnapshot(epoch=1, root=_node("root", children=[_node("button", "Old", ref="e1")]))
    current = AXSnapshot(
        epoch=2,
        root=_node(
            "root",
            children=[_node("button", "Inserted", ref="e1"), _node("button", "Old", ref="e2")],
        ),
    )
    lines = render_snapshot_for_llm(current, previous).splitlines()
    old_line = next(line for line in lines if "Old" in line)
    inserted_line = next(line for line in lines if "Inserted" in line)
    assert not old_line.strip().startswith("*")  # unchanged element, not new
    assert inserted_line.strip().startswith("*[e1]")  # the truly-new one


def test_first_step_with_no_previous_marks_nothing_as_new() -> None:
    root = _node("root", children=[_node("button", "First", ref="e1")])
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root), previous=None)
    assert "*[e1]" not in text
    assert "[e1]" in text


def test_empty_tree_renders_placeholder() -> None:
    root = _node("root")
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root))
    assert text == "(empty page)"


def test_nested_children_are_indented() -> None:
    root = _node(
        "root",
        children=[_node("generic", "Section", ref="", children=[_node("link", "Item", ref="e9")])],
    )
    text = render_snapshot_for_llm(AXSnapshot(epoch=1, root=root))
    lines = [line for line in text.splitlines() if line.strip()]
    link_line = next(line for line in lines if "Item" in line)
    assert link_line.startswith("  ")  # nested one level under the generic
