"""Turn a fused `EnhancedDOMTreeNode` tree into the compact, indexed observation
the agent model reads -- ported from browser-use's `DOMTreeSerializer`.

Pipeline (each stage is a discrete, testable compression step; checklist L):

1. **simplify** -- drop non-content tags (script/style/head/...), collapse SVG,
   keep interactive + text-bearing + structural nodes, descend shadow roots and
   iframe content documents.
2. **paint-order occlusion** -- drop interactive nodes fully covered by opaque
   later-painted elements (`dom.paint_order`).
3. **containment dedup** -- drop an interactive node ~fully inside an interactive
   ancestor (button-in-button), keeping form controls / aria-labeled / role /
   onclick children.
4. **index** -- assign `selector_index = backend_node_id` to each surviving
   interactive node and build the `selector_map` (ref -> node) ref-resolution
   and the diff key on.

`serialize` returns the `selector_map` plus the rendered string (`dom.render`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentpilot.dom import render
from agentpilot.dom.clickable_elements import is_interactive
from agentpilot.dom.paint_order import PaintEntry, compute_occluded
from agentpilot.spi.dom_tree import DOMSelectorMap, EnhancedDOMTreeNode, NodeType
from agentpilot.spi.snapshot import BoundingBox

# Tags with no useful content for the agent -- pruned entirely.
_DISABLED_TAGS = frozenset(
    {"script", "style", "head", "meta", "link", "noscript", "title", "base", "template"}
)
_SVG_TAG = "svg"
_CONTAINMENT_THRESHOLD = 0.99
_FORM_CONTROL_TAGS = frozenset({"input", "select", "textarea", "option"})


@dataclass
class SimplifiedNode:
    """A retained node in the simplified tree, wrapping a fused node with the
    serializer's per-stage flags."""

    original: EnhancedDOMTreeNode
    children: list[SimplifiedNode] = field(default_factory=list)
    is_interactive: bool = False
    selector_index: int | None = None
    is_new: bool = False
    ignored_by_paint_order: bool = False
    excluded_by_parent: bool = False
    is_shadow_host: bool = False

    def text_content(self) -> str:
        """Own text for a kept non-interactive node -- text-node value, else
        empty (structural nodes contribute only indentation/children)."""

        if self.original.node_type == NodeType.TEXT_NODE:
            return self.original.node_value.strip()
        return ""


@dataclass
class SerializedDOM:
    selector_map: DOMSelectorMap
    llm_text: str


def _bounds(node: EnhancedDOMTreeNode) -> BoundingBox | None:
    if node.absolute_position is not None:
        return node.absolute_position
    return node.snapshot.bounds if node.snapshot else None


def _build_simplified(node: EnhancedDOMTreeNode) -> SimplifiedNode | None:
    """Recursively build the simplified subtree for `node`, or None if neither
    it nor any descendant is worth keeping."""

    if node.node_type == NodeType.TEXT_NODE:
        text = node.node_value.strip()
        return SimplifiedNode(original=node) if text else None

    if node.node_type not in (
        NodeType.ELEMENT_NODE,
        NodeType.DOCUMENT_NODE,
        NodeType.DOCUMENT_FRAGMENT_NODE,
    ):
        return None

    tag = node.tag_name
    if tag in _DISABLED_TAGS:
        return None

    interactive = is_interactive(node) and node.is_visible is not False

    children: list[SimplifiedNode] = []
    if tag != _SVG_TAG:  # collapse SVG internals
        descendants = list(node.children_and_shadow_roots)
        if node.content_document is not None:
            descendants.append(node.content_document)
        for child in descendants:
            simplified_child = _build_simplified(child)
            if simplified_child is not None:
                children.append(simplified_child)

    is_iframe = tag in ("iframe", "frame")
    is_shadow_host = bool(node.shadow_roots)
    if not (interactive or children or is_iframe or is_shadow_host):
        return None

    return SimplifiedNode(
        original=node,
        children=children,
        is_interactive=interactive,
        is_shadow_host=is_shadow_host,
    )


def _iter_simplified(root: SimplifiedNode):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _apply_paint_order(root: SimplifiedNode) -> None:
    entries: list[PaintEntry] = []
    for node in _iter_simplified(root):
        if not node.is_interactive:
            continue
        original = node.original
        bounds = _bounds(original)
        snapshot = original.snapshot
        if bounds is None or snapshot is None or snapshot.paint_order is None:
            continue
        styles = snapshot.computed_styles or {}
        try:
            opacity = float(styles.get("opacity", "1"))
        except (ValueError, TypeError):
            opacity = 1.0
        bg = styles.get("background-color", "rgba(0, 0, 0, 0)")
        entries.append(
            PaintEntry(
                key=original.backend_node_id,
                x=bounds.x,
                y=bounds.y,
                width=bounds.width,
                height=bounds.height,
                paint_order=snapshot.paint_order,
                opacity=opacity,
                background_transparent=bg in ("rgba(0, 0, 0, 0)", "transparent"),
                context=(original.session_id, original.frame_id),
            )
        )
    occluded = compute_occluded(entries)
    for node in _iter_simplified(root):
        if node.is_interactive and node.original.backend_node_id in occluded:
            node.ignored_by_paint_order = True


def _containment_ratio(child: BoundingBox, parent: BoundingBox) -> float:
    """Fraction of `child`'s area inside `parent`."""

    ix = max(child.x, parent.x)
    iy = max(child.y, parent.y)
    ax = min(child.x + child.width, parent.x + parent.width)
    ay = min(child.y + child.height, parent.y + parent.height)
    if ax <= ix or ay <= iy:
        return 0.0
    inter = (ax - ix) * (ay - iy)
    child_area = child.width * child.height
    return inter / child_area if child_area > 0 else 0.0


def _is_containment_exception(node: EnhancedDOMTreeNode) -> bool:
    """Interactive children we never dedupe away even when nested in another
    interactive element."""

    if node.tag_name in _FORM_CONTROL_TAGS:
        return True
    if node.attributes.get("aria-label"):
        return True
    if node.attributes.get("role"):
        return True
    return "onclick" in node.attributes


def _apply_containment(root: SimplifiedNode) -> None:
    """Mark an interactive node `excluded_by_parent` when it sits ~entirely
    inside a nearer interactive ancestor (button-in-button dedup)."""

    def walk(node: SimplifiedNode, interactive_ancestor: SimplifiedNode | None) -> None:
        next_ancestor = interactive_ancestor
        if node.is_interactive and not node.ignored_by_paint_order:
            child_bounds = _bounds(node.original)
            if (
                interactive_ancestor is not None
                and child_bounds is not None
                and not _is_containment_exception(node.original)
            ):
                parent_bounds = _bounds(interactive_ancestor.original)
                if (
                    parent_bounds is not None
                    and _containment_ratio(child_bounds, parent_bounds) >= _CONTAINMENT_THRESHOLD
                ):
                    node.excluded_by_parent = True
            if not node.excluded_by_parent:
                next_ancestor = node
        for child in node.children:
            walk(child, next_ancestor)

    walk(root, None)


def _assign_indices(root: SimplifiedNode, new_backend_ids: set[int]) -> DOMSelectorMap:
    selector_map: DOMSelectorMap = {}
    for node in _iter_simplified(root):
        if node.is_interactive and not node.ignored_by_paint_order and not node.excluded_by_parent:
            index = node.original.backend_node_id
            node.selector_index = index
            node.is_new = index in new_backend_ids
            selector_map[index] = node.original
    return selector_map


def serialize(
    root: EnhancedDOMTreeNode,
    *,
    new_backend_ids: set[int] | None = None,
    include_attributes: tuple[str, ...] = render.DEFAULT_INCLUDE_ATTRIBUTES,
    max_length: int | None = None,
) -> SerializedDOM:
    """Run the full pipeline and return the `selector_map` + rendered text.
    `new_backend_ids` (from the diff) drive the inline `*` new-element markers."""

    simplified = _build_simplified(root)
    if simplified is None:
        return SerializedDOM(selector_map={}, llm_text="(empty page)")

    _apply_paint_order(simplified)
    _apply_containment(simplified)
    selector_map = _assign_indices(simplified, new_backend_ids or set())
    text = render.render_tree(
        simplified, include_attributes=include_attributes, max_length=max_length
    )
    return SerializedDOM(selector_map=selector_map, llm_text=text or "(empty page)")
