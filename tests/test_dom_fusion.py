"""Unit tests for `agentpilot.driver.dom_fusion.build_snapshot_lookup` --
pure parsing of a synthetic CDP DOMSnapshot payload. No browser."""

from __future__ import annotations

from agentpilot.driver.dom_fusion import REQUIRED_COMPUTED_STYLES, build_snapshot_lookup


def _snapshot() -> dict:
    # Two nodes: backend 10 (not clickable) and backend 20 (clickable). Styles
    # are positional string-indices into `strings`, in REQUIRED_COMPUTED_STYLES
    # order -> here just display + visibility.
    strings = ["block", "visible"]
    return {
        "strings": strings,
        "documents": [
            {
                "nodes": {
                    "backendNodeId": [10, 20],
                    "isClickable": {"index": [1]},  # snapshot index 1 -> backend 20
                },
                "layout": {
                    "nodeIndex": [0, 1],
                    "bounds": [[0, 0, 100, 50], [10, 20, 30, 40]],
                    "styles": [[0, 1], [0, 1]],
                    "paintOrders": [1, 2],
                },
            }
        ],
    }


def test_backend_ids_mapped_with_dpr_scaled_bounds() -> None:
    lookup = build_snapshot_lookup(_snapshot(), device_pixel_ratio=2.0)
    assert set(lookup) == {10, 20}

    a = lookup[10]
    assert a.bounds is not None
    # 100x50 at dpr 2 -> 50x25 CSS px.
    assert (a.bounds.width, a.bounds.height) == (50.0, 25.0)
    assert a.computed_styles["display"] == "block"
    assert a.computed_styles["visibility"] == "visible"
    assert a.paint_order == 1
    assert a.is_clickable is False


def test_clickable_flag_from_rare_boolean_index() -> None:
    lookup = build_snapshot_lookup(_snapshot())
    assert lookup[20].is_clickable is True
    assert lookup[10].is_clickable is False


def test_node_without_layout_gets_clickable_only() -> None:
    snap = _snapshot()
    # Drop layout mapping for node index 1 (backend 20): no bounds/styles.
    snap["documents"][0]["layout"]["nodeIndex"] = [0]
    lookup = build_snapshot_lookup(snap)
    assert lookup[20].bounds is None
    assert lookup[20].computed_styles == {}
    # Still flagged clickable from the rare-boolean data.
    assert lookup[20].is_clickable is True


def test_empty_snapshot_is_empty_lookup() -> None:
    assert build_snapshot_lookup({}) == {}
    assert build_snapshot_lookup({"documents": [], "strings": []}) == {}


def test_required_styles_order_is_stable() -> None:
    # The positional styles parse depends on this order -- guard against drift.
    assert REQUIRED_COMPUTED_STYLES[0] == "display"
    assert REQUIRED_COMPUTED_STYLES[1] == "visibility"
