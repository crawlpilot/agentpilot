"""Unit tests for `agentpilot.agent.observation.identity_fingerprint` -- the
loop's page-stagnation fingerprint over the *stable identities* of the
interactive elements: stable across identical interactive sets, blind to
ephemeral refs/bboxes and to reordering, but sensitive to a real change in the
interactive set (a name change or an added element)."""

from __future__ import annotations

from agentpilot.agent.observation import identity_fingerprint
from agentpilot.spi.geometry import BoundingBox
from tests.fusion_fixtures import fnode


def _tree(*children):
    return fnode("root", children=list(children))


def test_identical_trees_hash_equal() -> None:
    a = _tree(fnode("button", "Buy", ref="e1"), fnode("link", "Home", ref="e2"))
    b = _tree(fnode("button", "Buy", ref="e1"), fnode("link", "Home", ref="e2"))
    assert identity_fingerprint(a) == identity_fingerprint(b)


def test_ref_and_bbox_do_not_affect_fingerprint() -> None:
    # Same role/name structure; only the ephemeral ref and bbox differ.
    a = _tree(fnode("button", "Buy", ref="e1", bbox=BoundingBox(0, 0, 5, 5)))
    b = _tree(fnode("button", "Buy", ref="e999", bbox=BoundingBox(400, 400, 5, 5)))
    assert identity_fingerprint(a) == identity_fingerprint(b)


def test_content_change_flips_fingerprint() -> None:
    a = _tree(fnode("button", "Buy", ref="e1"))
    b = _tree(fnode("button", "Sold out", ref="e1"))  # name changed
    assert identity_fingerprint(a) != identity_fingerprint(b)


def test_added_node_flips_fingerprint() -> None:
    a = _tree(fnode("button", "Buy", ref="e1"))
    b = _tree(fnode("button", "Buy", ref="e1"), fnode("link", "More", ref="e2"))
    assert identity_fingerprint(a) != identity_fingerprint(b)


def test_reordering_the_same_interactive_set_is_stable() -> None:
    # The fingerprint is over the *set* of stable identities (sorted), so a
    # pure reorder of the same interactive elements is not a page change.
    a = _tree(fnode("button", "A", ref="e1"), fnode("button", "B", ref="e2"))
    b = _tree(fnode("button", "B", ref="e2"), fnode("button", "A", ref="e1"))
    assert identity_fingerprint(a) == identity_fingerprint(b)
