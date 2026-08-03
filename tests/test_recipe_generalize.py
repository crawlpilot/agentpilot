"""Pure unit tests for `agentpilot.recipe.generalize` -- generalizing one
clicked representative option into a `RepeatSpec` matching its sibling set,
given a static `AXSnapshot` fixture. No browser."""

from __future__ import annotations

from agentpilot.recipe.generalize import generalize_option_locator, single_option_fallback
from agentpilot.spi.snapshot import AXSnapshot, SnapshotNode


def _node(role: str, name: str = "", ref: str = "", children=None) -> SnapshotNode:
    return SnapshotNode(epoch=1, ref=ref, role=role, name=name, children=children or [])


def _size_swatch_snapshot() -> AXSnapshot:
    swatches = _node(
        "generic",
        children=[
            _node("button", "S", ref="e10"),
            _node("button", "M", ref="e11"),
            _node("button", "L", ref="e12"),
        ],
    )
    return AXSnapshot(epoch=1, root=_node("root", children=[swatches]))


def test_generalize_finds_sibling_options_sharing_the_clicked_nodes_role() -> None:
    snapshot = _size_swatch_snapshot()
    repeat = generalize_option_locator(
        snapshot=snapshot, clicked_ref="e11", array_field="variants", max_iterations=20
    )
    assert repeat is not None
    assert repeat.array_field == "variants"
    assert repeat.max_iterations == 20
    assert repeat.option_locator.source == "ax_role"
    assert repeat.option_locator.role == "button"
    assert set(repeat.option_locator.name_in or []) == {"S", "M", "L"}


def test_generalize_returns_none_when_clicked_ref_unresolvable() -> None:
    snapshot = _size_swatch_snapshot()
    assert (
        generalize_option_locator(
            snapshot=snapshot, clicked_ref="missing", array_field="variants", max_iterations=20
        )
        is None
    )


def test_generalize_returns_none_when_only_one_sibling_exists() -> None:
    root = _node("root", children=[_node("button", "Only", ref="e1")])
    snapshot = AXSnapshot(epoch=1, root=root)
    assert (
        generalize_option_locator(
            snapshot=snapshot, clicked_ref="e1", array_field="variants", max_iterations=20
        )
        is None
    )


def test_single_option_fallback_covers_just_the_one_option_found() -> None:
    root = _node("root", children=[_node("button", "Only", ref="e1")])
    snapshot = AXSnapshot(epoch=1, root=root)
    repeat = single_option_fallback(clicked_ref="e1", snapshot=snapshot, array_field="variants")
    assert repeat is not None
    assert repeat.max_iterations == 1
    assert repeat.option_locator.name_in == ["Only"]


def test_single_option_fallback_returns_none_for_unresolvable_ref() -> None:
    root = _node("root", children=[])
    snapshot = AXSnapshot(epoch=1, root=root)
    assert single_option_fallback(clicked_ref="missing", snapshot=snapshot, array_field="v") is None
