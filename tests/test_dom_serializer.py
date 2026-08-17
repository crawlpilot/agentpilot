"""Unit tests for the serializer pipeline (`agentpilot.dom.paint_order` +
`agentpilot.dom.serializer` + `agentpilot.dom.render`): occlusion, containment
dedup, backend-id indexing, attribute compression, password redaction, and
truncation. Synthetic fused trees; no browser."""

from __future__ import annotations

from agentpilot.dom.paint_order import PaintEntry, Rect, RectUnionPure, compute_occluded
from agentpilot.dom.serializer import serialize
from agentpilot.driver.dom_fusion import LayoutInfo
from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from agentpilot.spi.geometry import BoundingBox

# --------------------------------------------------------------------- paint order


def test_rect_union_covers_when_tiled() -> None:
    u = RectUnionPure()
    u.add(Rect(0, 0, 10, 10))
    u.add(Rect(10, 0, 20, 10))
    assert u.contains(Rect(2, 2, 18, 8))  # spanning both tiles
    assert not u.contains(Rect(2, 2, 22, 8))  # pokes past the union


def test_compute_occluded_front_covers_back() -> None:
    back = PaintEntry(key=1, x=0, y=0, width=100, height=100, paint_order=1)
    front = PaintEntry(key=2, x=0, y=0, width=100, height=100, paint_order=5)
    assert compute_occluded([back, front]) == {1}


def test_translucent_front_does_not_occlude() -> None:
    back = PaintEntry(key=1, x=0, y=0, width=100, height=100, paint_order=1)
    front = PaintEntry(key=2, x=0, y=0, width=100, height=100, paint_order=5, opacity=0.3)
    assert compute_occluded([back, front]) == set()


# --------------------------------------------------------------------- fixtures


def _node(
    tag: str,
    backend: int,
    *,
    attrs: dict[str, str] | None = None,
    ax_role: str | None = None,
    ax_name: str = "",
    node_type: NodeType = NodeType.ELEMENT_NODE,
    value: str = "",
    bounds: BoundingBox | None = None,
    paint_order: int | None = None,
    visible: bool = True,
    bg: str | None = None,
    cursor: str | None = None,
) -> EnhancedDOMTreeNode:
    snapshot = None
    if bounds is not None or paint_order is not None or cursor is not None:
        styles = {"display": "block"}
        if bg is not None:
            styles["background-color"] = bg
        snapshot = LayoutInfo(
            bounds=bounds,
            paint_order=paint_order,
            cursor_style=cursor,
            computed_styles=styles,
        )
    return EnhancedDOMTreeNode(
        node_id=backend,
        backend_node_id=backend,
        node_type=node_type,
        node_name=tag.upper() if node_type == NodeType.ELEMENT_NODE else tag,
        node_value=value,
        attributes=attrs or {},
        is_visible=visible,
        absolute_position=bounds,
        snapshot=snapshot,
        ax_node=EnhancedAXNode(role=ax_role, name=ax_name) if ax_role or ax_name else None,
    )


def _child(parent: EnhancedDOMTreeNode, node: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
    node.parent_node = parent
    parent.children_nodes.append(node)
    return node


# --------------------------------------------------------------------- serializer


def test_disabled_tags_and_svg_collapsed() -> None:
    body = _node("body", 1)
    _child(body, _node("script", 2, value="x"))
    _child(body, _node("style", 3))
    svg = _child(body, _node("svg", 4, ax_role="img"))
    _child(svg, _node("path", 5))  # svg internals must not appear
    btn = _child(body, _node("button", 6, ax_name="Go", bounds=BoundingBox(0, 0, 40, 20)))
    _ = btn

    result = serialize(body)
    assert "script" not in result.llm_text and "style" not in result.llm_text
    assert "path" not in result.llm_text
    assert "[e6]" in result.llm_text  # the button survived


def test_index_is_backend_node_id_and_new_marker() -> None:
    body = _node("body", 1)
    _child(body, _node("button", 42, ax_name="Buy", bounds=BoundingBox(0, 0, 50, 20)))
    result = serialize(body, new_backend_ids={42})
    assert set(result.selector_map) == {42}
    assert result.selector_map[42].ax_name == "Buy"
    assert "*[e42]" in result.llm_text  # NEW marker


def test_containment_dedup_button_in_button() -> None:
    # Outer clickable div fully containing an inner clickable span with no
    # keep-exception attrs (interactive only via ax role) -> inner is deduped.
    outer = _node("div", 1, attrs={"onclick": "f()"}, bounds=BoundingBox(0, 0, 200, 50))
    inner = _child(outer, _node("span", 2, ax_role="button", bounds=BoundingBox(5, 5, 50, 20)))
    _ = inner
    result = serialize(outer)
    assert 1 in result.selector_map
    assert 2 not in result.selector_map  # contained -> excluded


def test_paint_order_occlusion_removes_covered_interactive() -> None:
    body = _node("body", 1)
    hidden = _child(
        body,
        _node("button", 2, ax_name="Behind", bounds=BoundingBox(0, 0, 100, 100), paint_order=1),
    )
    overlay = _child(
        body,
        _node(
            "button",
            3,
            ax_name="Overlay",
            bounds=BoundingBox(0, 0, 100, 100),
            paint_order=9,
            bg="rgb(255,255,255)",  # opaque -> actually hides what's behind
        ),
    )
    _ = (hidden, overlay)
    result = serialize(body)
    assert 3 in result.selector_map
    assert 2 not in result.selector_map  # occluded


def test_attribute_whitelist_and_password_redaction() -> None:
    body = _node("body", 1)
    _child(
        body,
        _node(
            "input",
            2,
            attrs={"type": "password", "value": "hunter2", "data-secret": "leak"},
            bounds=BoundingBox(0, 0, 100, 20),
        ),
    )
    text = serialize(body).llm_text
    assert "hunter2" not in text  # redacted
    assert "<redacted>" in text
    assert "data-secret" not in text  # not in the whitelist


def test_truncation_marker_added_when_over_budget() -> None:
    body = _node("body", 1)
    for i in range(50):
        _child(body, _node("button", 100 + i, ax_name=f"Item {i}", bounds=BoundingBox(0, i, 80, 1)))
    text = serialize(body, max_length=200).llm_text
    assert len(text) <= 200
    assert "truncated" in text


def test_compression_reduces_node_count() -> None:
    # 3 real interactive buttons buried under wrapper/structural divs + noise.
    body = _node("body", 1)
    for i in range(3):
        wrapper = _child(body, _node("div", 10 + i))  # structural, no text
        _child(wrapper, _node("button", 20 + i, ax_name=f"B{i}", bounds=BoundingBox(0, i, 40, 10)))
    _child(body, _node("script", 99, value="noise"))
    result = serialize(body)
    # Only the 3 buttons get indexed; wrappers/script contribute no refs.
    assert set(result.selector_map) == {20, 21, 22}
    assert result.llm_text.count("[e") == 3
