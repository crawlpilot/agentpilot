"""P2 (slice 1): pure parsing of a CDP `DOMSnapshot.captureSnapshot` payload
into a per-`backendNodeId` layout lookup -- the data foundation the CDP DOM
fusion pipeline builds on.

Deliberately pure and driver-free: given the raw snapshot dict, produce
`{backend_node_id: LayoutInfo}`, unit-testable against synthetic payloads with
no live page or CDP session. Runtime-free by construction (it only reads
DOMSnapshot output), so it respects Patchright's no-Runtime stealth posture.

Not yet wired into the live snapshot path -- later slices fuse this with the
DOM + accessibility trees and index elements by `backendNodeId` (stable across
snapshots, unlike Playwright's re-minted `aria-ref`), behind a
`SnapshotAction.engine` flag. Ported from browser-use's
`dom/enhanced_snapshot.py::build_snapshot_lookup`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentpilot.spi.snapshot import BoundingBox

# The only computed styles the fusion pipeline consumes (visibility, occlusion,
# scrollability, interactivity). Kept minimal -- requesting every style makes
# Chrome's DOMSnapshot enormous and can crash on heavy pages. Order matters:
# `layout.styles[i]` is a positional list of string-indices in this same order.
REQUIRED_COMPUTED_STYLES = [
    "display",
    "visibility",
    "opacity",
    "overflow",
    "overflow-x",
    "overflow-y",
    "cursor",
    "pointer-events",
    "position",
    "background-color",
]


@dataclass
class LayoutInfo:
    """Per-node layout/paint data extracted from a DOMSnapshot document."""

    bounds: BoundingBox | None = None
    computed_styles: dict[str, str] = field(default_factory=dict)
    paint_order: int | None = None
    is_clickable: bool = False
    cursor_style: str | None = None
    client_rects: BoundingBox | None = None
    scroll_rects: BoundingBox | None = None


def _parse_styles(strings: list[str], style_indices: list[int]) -> dict[str, str]:
    styles: dict[str, str] = {}
    for i, idx in enumerate(style_indices):
        if i < len(REQUIRED_COMPUTED_STYLES) and 0 <= idx < len(strings):
            styles[REQUIRED_COMPUTED_STYLES[i]] = strings[idx]
    return styles


def _rect(values: list[float] | None, dpr: float) -> BoundingBox | None:
    if not values or len(values) < 4:
        return None
    return BoundingBox(
        x=values[0] / dpr, y=values[1] / dpr, width=values[2] / dpr, height=values[3] / dpr
    )


def build_snapshot_lookup(
    snapshot: dict[str, Any], device_pixel_ratio: float = 1.0
) -> dict[int, LayoutInfo]:
    """Map every `backendNodeId` in the snapshot to its `LayoutInfo`.

    CDP bounds are in device pixels; they're divided by `device_pixel_ratio`
    to CSS pixels (matching the coordinate space Playwright/aria bboxes use).
    `clientRects`/`scrollRects` are left in their raw space, as browser-use
    does. When a snapshot node has no corresponding layout node (not laid
    out / not visible), only `is_clickable` is populated."""

    lookup: dict[int, LayoutInfo] = {}
    dpr = device_pixel_ratio or 1.0

    for document in snapshot.get("documents") or []:
        strings: list[str] = snapshot.get("strings") or []
        nodes = document.get("nodes", {})
        layout = document.get("layout", {})

        backend_ids: list[int] = nodes.get("backendNodeId", [])

        # snapshot-node-index -> layout-index (first occurrence wins).
        layout_by_node: dict[int, int] = {}
        for li, snap_idx in enumerate(layout.get("nodeIndex", [])):
            layout_by_node.setdefault(snap_idx, li)

        # `isClickable` is CDP "rare boolean data": a sparse list of the
        # snapshot indices that are clickable. Set-ify for O(1) membership.
        clickable_indices: set[int] = set((nodes.get("isClickable") or {}).get("index", []))

        bounds_arr = layout.get("bounds", [])
        styles_arr = layout.get("styles", [])
        paint_orders = layout.get("paintOrders", [])
        client_rects_arr = layout.get("clientRects", [])
        scroll_rects_arr = layout.get("scrollRects", [])

        for snap_idx, backend_id in enumerate(backend_ids):
            info = LayoutInfo(is_clickable=snap_idx in clickable_indices)
            layout_idx = layout_by_node.get(snap_idx)
            if layout_idx is not None:
                if layout_idx < len(bounds_arr):
                    info.bounds = _rect(bounds_arr[layout_idx], dpr)
                if layout_idx < len(styles_arr):
                    info.computed_styles = _parse_styles(strings, styles_arr[layout_idx])
                    info.cursor_style = info.computed_styles.get("cursor")
                if layout_idx < len(paint_orders):
                    info.paint_order = paint_orders[layout_idx]
                if layout_idx < len(client_rects_arr):
                    info.client_rects = _rect(client_rects_arr[layout_idx], 1.0)
                if layout_idx < len(scroll_rects_arr):
                    info.scroll_rects = _rect(scroll_rects_arr[layout_idx], 1.0)
            lookup[backend_id] = info

    return lookup
