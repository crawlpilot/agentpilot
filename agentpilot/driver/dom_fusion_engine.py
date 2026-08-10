"""CDP DOM fusion engine -- merges the three CDP trees (`DOM.getDocument`,
`DOMSnapshot.captureSnapshot`, `Accessibility.getFullAXTree`) into one
`EnhancedDOMTreeNode` graph keyed on `backendNodeId`. Ported from browser-use's
`DomService.get_dom_tree` / `_construct_enhanced_node` and Browser4's
`CDPSnapshotService.buildMergedDOMTreeNode`.

Split in two so the hard part is testable without a browser:

- `build_enhanced_tree(...)` and `build_ax_lookup(...)` are **pure**: given the
  raw CDP dicts (+ the `driver.dom_fusion` layout lookup) they build the fused
  tree with no async, no CDP session -- unit-testable against synthetic
  payloads. All the fiddly logic (attribute flattening, shadow/iframe descent,
  frame-offset accumulation, viewport visibility) lives here.
- `capture_fused_tree(...)` is the thin async wrapper that fires the CDP calls
  in parallel on one reused session and calls the pure builder.

**Stealth:** `DOM` / `DOMSnapshot` / `Accessibility` / `Page` are all
non-Runtime domains. The only Runtime-using signal, `getEventListeners`
(`has_js_click_listener`), is fetched only when `no_runtime` is False -- under
the UI-driven stealth mode the set is empty and the tree is Runtime-free.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from agentpilot.driver.dom_fusion import (
    REQUIRED_COMPUTED_STYLES,
    LayoutInfo,
    build_snapshot_lookup,
)
from agentpilot.spi.dom_tree import EnhancedAXNode, EnhancedDOMTreeNode, NodeType
from agentpilot.spi.snapshot import BoundingBox

if TYPE_CHECKING:
    from patchright.async_api import CDPSession

# AX properties the interactivity/diff logic actually reads -- everything else
# Chrome reports is dropped to keep nodes small.
_KEPT_AX_PROPERTIES = frozenset(
    {
        "disabled",
        "hidden",
        "focusable",
        "editable",
        "settable",
        "checked",
        "expanded",
        "pressed",
        "selected",
        "required",
        "autocomplete",
        "keyshortcuts",
        "level",
        "valuetext",
    }
)

_VIEWPORT_THRESHOLD_PX = 1000
"""Elements within this many px of a containing frame's viewport count as
visible (browser-use `viewport_threshold`, Browser4 `VIEWPORT_VISIBILITY_MARGIN_PX`)."""


def build_ax_lookup(ax_tree: dict[str, Any]) -> dict[int, EnhancedAXNode]:
    """`backendDOMNodeId -> EnhancedAXNode` from an `Accessibility.getFullAXTree`
    payload. Flattens Chrome's `{value: {value: x}}` wrappers and keeps only the
    AX properties the pipeline consumes."""

    lookup: dict[int, EnhancedAXNode] = {}
    for ax_node in ax_tree.get("nodes", []):
        backend_id = ax_node.get("backendDOMNodeId")
        if backend_id is None:
            continue
        properties: dict[str, str | bool] = {}
        for prop in ax_node.get("properties") or []:
            name = prop.get("name")
            if name not in _KEPT_AX_PROPERTIES:
                continue
            value = (prop.get("value") or {}).get("value")
            if value is not None:
                properties[name] = value
        lookup[backend_id] = EnhancedAXNode(
            role=(ax_node.get("role") or {}).get("value"),
            name=(ax_node.get("name") or {}).get("value"),
            description=(ax_node.get("description") or {}).get("value"),
            properties=properties,
        )
    return lookup


def _attributes_to_dict(raw: list[str] | None) -> dict[str, str]:
    """CDP DOM attributes arrive as a flat `[k0, v0, k1, v1, ...]` array."""

    if not raw:
        return {}
    return {raw[i]: raw[i + 1] for i in range(0, len(raw) - 1, 2)}


def build_enhanced_tree(
    dom_document: dict[str, Any],
    snapshot_lookup: dict[int, LayoutInfo],
    ax_lookup: dict[int, EnhancedAXNode],
    *,
    target_id: str | None = None,
    session_id: str | None = None,
    js_click_backend_ids: set[int] | None = None,
    viewport_threshold: int | None = _VIEWPORT_THRESHOLD_PX,
) -> EnhancedDOMTreeNode:
    """Recursively fuse a `DOM.getDocument` document into an
    `EnhancedDOMTreeNode` graph. Pure and synchronous.

    Descends `contentDocument` (same-origin iframes) and `shadowRoots`,
    accumulates per-frame coordinate offsets so `absolute_position` is in
    document space, and marks each node's `is_visible` from CSS + the frame
    viewport stack."""

    js_ids = js_click_backend_ids or set()
    memo: dict[int, EnhancedDOMTreeNode] = {}

    def construct(
        node: dict[str, Any],
        html_frames: list[EnhancedDOMTreeNode],
        offset_x: float,
        offset_y: float,
    ) -> EnhancedDOMTreeNode:
        node_id = node["nodeId"]
        cached = memo.get(node_id)
        if cached is not None:
            return cached

        backend_id = node["backendNodeId"]
        snapshot = snapshot_lookup.get(backend_id)

        absolute_position = None
        if snapshot is not None and snapshot.bounds is not None:
            absolute_position = BoundingBox(
                x=snapshot.bounds.x + offset_x,
                y=snapshot.bounds.y + offset_y,
                width=snapshot.bounds.width,
                height=snapshot.bounds.height,
            )

        enhanced = EnhancedDOMTreeNode(
            node_id=node_id,
            backend_node_id=backend_id,
            node_type=NodeType(node["nodeType"]),
            node_name=node["nodeName"],
            node_value=node.get("nodeValue", ""),
            attributes=_attributes_to_dict(node.get("attributes")),
            is_scrollable=node.get("isScrollable"),
            frame_id=node.get("frameId"),
            session_id=session_id,
            target_id=target_id,
            shadow_root_type=node.get("shadowRootType"),
            ax_node=ax_lookup.get(backend_id),
            snapshot=snapshot,
            has_js_click_listener=backend_id in js_ids,
            absolute_position=absolute_position,
        )
        memo[node_id] = enhanced

        # This node's own frame context, extended for its descendants.
        frames = html_frames
        child_offset_x, child_offset_y = offset_x, offset_y
        name_upper = node["nodeName"].upper()

        is_frame_root = (
            enhanced.node_type == NodeType.ELEMENT_NODE
            and name_upper == "HTML"
            and node.get("frameId") is not None
        )
        if is_frame_root:
            frames = [*html_frames, enhanced]
            if snapshot is not None and snapshot.scroll_rects is not None:
                child_offset_x -= snapshot.scroll_rects.x
                child_offset_y -= snapshot.scroll_rects.y
        elif (
            name_upper in ("IFRAME", "FRAME")
            and snapshot is not None
            and snapshot.bounds is not None
        ):
            frames = [*html_frames, enhanced]
            child_offset_x += snapshot.bounds.x
            child_offset_y += snapshot.bounds.y

        content_document = node.get("contentDocument")
        if content_document:
            child = construct(content_document, frames, child_offset_x, child_offset_y)
            child.parent_node = enhanced
            enhanced.content_document = child

        shadow_root_ids: set[int] = set()
        for shadow_root in node.get("shadowRoots") or []:
            shadow_root_ids.add(shadow_root["nodeId"])
            child = construct(shadow_root, frames, child_offset_x, child_offset_y)
            child.parent_node = enhanced
            enhanced.shadow_roots.append(child)

        for raw_child in node.get("children") or []:
            if raw_child["nodeId"] in shadow_root_ids:
                continue  # shadow roots live only in `shadow_roots`
            child = construct(raw_child, frames, child_offset_x, child_offset_y)
            child.parent_node = enhanced
            enhanced.children_nodes.append(child)

        enhanced.is_visible = _is_visible(enhanced, frames, viewport_threshold)
        return enhanced

    root = dom_document["root"] if "root" in dom_document else dom_document
    return construct(root, [], 0.0, 0.0)


def _is_visible(
    node: EnhancedDOMTreeNode,
    html_frames: list[EnhancedDOMTreeNode],
    viewport_threshold: int | None,
) -> bool:
    """CSS visibility gate + viewport-intersection across every containing
    frame (ported from browser-use `is_element_visible_according_to_all_parents`).
    `viewport_threshold=None` disables the viewport check (CSS-only)."""

    snapshot = node.snapshot
    if snapshot is None or snapshot.bounds is None:
        return False

    styles = snapshot.computed_styles or {}
    if styles.get("display", "").lower() == "none":
        return False
    if styles.get("visibility", "").lower() == "hidden":
        return False
    with contextlib.suppress(ValueError, TypeError):
        if float(styles.get("opacity", "1")) <= 0:
            return False

    if viewport_threshold is None:
        return True

    bx = snapshot.bounds.x
    by = snapshot.bounds.y
    bw = snapshot.bounds.width
    bh = snapshot.bounds.height

    for frame in reversed(html_frames):
        if frame is node:
            continue
        fsnap = frame.snapshot
        if fsnap is None:
            continue
        name_upper = frame.node_name.upper()
        if name_upper in ("IFRAME", "FRAME") and fsnap.bounds is not None:
            # Undo the offset added while descending into this iframe.
            bx += fsnap.bounds.x
            by += fsnap.bounds.y
        if (
            frame.node_name == "HTML"
            and fsnap.scroll_rects is not None
            and fsnap.client_rects is not None
        ):
            adjusted_x = bx - fsnap.scroll_rects.x
            adjusted_y = by - fsnap.scroll_rects.y
            viewport_right = fsnap.client_rects.width
            viewport_bottom = fsnap.client_rects.height
            intersects = (
                adjusted_x < viewport_right
                and adjusted_x + bw > 0
                and adjusted_y < viewport_bottom + viewport_threshold
                and adjusted_y + bh > -viewport_threshold
            )
            if not intersects:
                return False
            bx = adjusted_x
            by = adjusted_y

    return True


async def _detect_js_click_listeners(cdp: CDPSession) -> set[int]:
    """Backend node ids of elements with click/mouse/pointer JS listeners, via
    the DevTools-only `getEventListeners` (needs `Runtime` + command-line API).
    Only called when Runtime is permitted. Best-effort: any failure -> empty
    set, so a missing capability never fails the snapshot. Bounded like
    browser-use (bail on huge pages, cap resolved elements)."""

    _CLICK_EVENTS = {"click", "mousedown", "mouseup", "pointerdown", "pointerup"}
    _MAX_ELEMENTS = 100

    try:
        doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        # Resolve the document to a JS object so getEventListeners can walk it.
        evaluated = await cdp.send(
            "Runtime.evaluate",
            {
                "expression": "document.querySelectorAll('*')",
                "includeCommandLineAPI": True,
            },
        )
    except Exception:
        return set()

    object_id = (evaluated.get("result") or {}).get("objectId")
    if object_id is None:
        return set()

    try:
        props = await cdp.send(
            "Runtime.getProperties", {"objectId": object_id, "ownProperties": True}
        )
    except Exception:
        return set()

    backend_ids: set[int] = set()
    count = 0
    for prop in props.get("result", []):
        value = prop.get("value") or {}
        el_object_id = value.get("objectId")
        if el_object_id is None or value.get("subtype") == "null":
            continue
        if count >= _MAX_ELEMENTS:
            break
        try:
            listeners = await cdp.send("DOMDebugger.getEventListeners", {"objectId": el_object_id})
        except Exception:
            continue
        if any(li.get("type") in _CLICK_EVENTS for li in listeners.get("listeners", [])):
            with contextlib.suppress(Exception):
                described = await cdp.send("DOM.describeNode", {"objectId": el_object_id})
                backend = (described.get("node") or {}).get("backendNodeId")
                if backend is not None:
                    backend_ids.add(backend)
                    count += 1
    _ = doc  # doc fetch validates the domain is enabled; ids come via describeNode
    return backend_ids


async def capture_fused_tree(
    cdp: CDPSession,
    *,
    target_id: str | None = None,
    session_id: str | None = None,
    no_runtime: bool = False,
    viewport_threshold: int | None = _VIEWPORT_THRESHOLD_PX,
) -> EnhancedDOMTreeNode:
    """Fire the three tree fetches in parallel on one reused CDP session and
    fuse them. `no_runtime=True` skips `getEventListeners` (stealth mode)."""

    dom_task = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    snapshot_task = cdp.send(
        "DOMSnapshot.captureSnapshot",
        {
            "computedStyles": REQUIRED_COMPUTED_STYLES,
            "includePaintOrder": True,
            "includeDOMRects": True,
        },
    )
    ax_task = cdp.send("Accessibility.getFullAXTree", {})
    metrics_task = cdp.send("Page.getLayoutMetrics")

    dom_doc, snapshot, ax_tree, metrics = await asyncio.gather(
        dom_task, snapshot_task, ax_task, metrics_task
    )

    device_pixel_ratio = float(
        (metrics.get("cssVisualViewport") or {}).get("scale")
        or (metrics.get("visualViewport") or {}).get("scale")
        or 1.0
    )
    snapshot_lookup = build_snapshot_lookup(snapshot, device_pixel_ratio)
    ax_lookup = build_ax_lookup(ax_tree)

    js_ids: set[int] = set()
    if not no_runtime:
        js_ids = await _detect_js_click_listeners(cdp)

    return build_enhanced_tree(
        dom_doc,
        snapshot_lookup,
        ax_lookup,
        target_id=target_id,
        session_id=session_id,
        js_click_backend_ids=js_ids,
        viewport_threshold=viewport_threshold,
    )
