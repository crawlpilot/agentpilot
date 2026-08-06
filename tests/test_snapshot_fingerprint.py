"""Unit tests for `AXSnapshot.fingerprint` -- stable across identical trees,
sensitive to real content changes, and blind to ephemeral refs/bboxes."""

from __future__ import annotations

from agentpilot.spi.snapshot import AXSnapshot, BoundingBox, SnapshotNode


def _n(role: str, name: str = "", ref: str = "", children=None, bbox=None) -> SnapshotNode:
    return SnapshotNode(epoch=1, ref=ref, role=role, name=name, children=children or [], bbox=bbox)


def _tree(*children: SnapshotNode) -> AXSnapshot:
    return AXSnapshot(epoch=1, root=_n("root", children=list(children)))


def test_identical_trees_hash_equal() -> None:
    a = _tree(_n("button", "Buy", ref="e1"), _n("link", "Home", ref="e2"))
    b = _tree(_n("button", "Buy", ref="e1"), _n("link", "Home", ref="e2"))
    assert a.fingerprint() == b.fingerprint()


def test_ref_and_bbox_do_not_affect_fingerprint() -> None:
    # Same role/name structure; only the ephemeral ref and bbox differ.
    a = _tree(_n("button", "Buy", ref="e1", bbox=BoundingBox(0, 0, 5, 5)))
    b = _tree(_n("button", "Buy", ref="e999", bbox=BoundingBox(400, 400, 5, 5)))
    assert a.fingerprint() == b.fingerprint()


def test_content_change_flips_fingerprint() -> None:
    a = _tree(_n("button", "Buy", ref="e1"))
    b = _tree(_n("button", "Sold out", ref="e1"))  # name changed
    assert a.fingerprint() != b.fingerprint()


def test_added_node_flips_fingerprint() -> None:
    a = _tree(_n("button", "Buy", ref="e1"))
    b = _tree(_n("button", "Buy", ref="e1"), _n("link", "More", ref="e2"))
    assert a.fingerprint() != b.fingerprint()


def test_structure_is_order_sensitive() -> None:
    a = _tree(_n("button", "A", ref="e1"), _n("button", "B", ref="e2"))
    b = _tree(_n("button", "B", ref="e2"), _n("button", "A", ref="e1"))
    assert a.fingerprint() != b.fingerprint()
