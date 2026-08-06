"""The enriched, fused DOM node -- the single node type the CDP fusion pipeline
produces and the serializer / change-diff / ref-resolution all consume. Ported
from browser-use's `EnhancedDOMTreeNode` (`dom/views.py`) and Browser4's
`MergedDOMTreeNode`, trimmed to what agentpilot's pipeline actually uses.

Each node fuses the three CDP trees keyed on `backendNodeId`:
- **DOM** (`DOM.getDocument`): structure, attributes, shadow roots, iframe
  content documents.
- **Snapshot** (`DOMSnapshot.captureSnapshot`, via `driver.dom_fusion`): layout
  bounds, computed styles, paint order, cursor, `isClickable`.
- **Accessibility** (`Accessibility.getFullAXTree`): role, name, state.

Identity is structural (`element_hash` / `stable_hash` from `spi.hashing`),
keyed on the stable `backendNodeId` -- never the re-minted Playwright aria-ref --
so a node is the *same* node across snapshots even as the page mutates. That is
what makes cross-step change detection and history replay possible.

Layering note: `snapshot` is typed as `LayoutInfo` for editors but imported only
under `TYPE_CHECKING`; with `from __future__ import annotations` the hint is a
string and never evaluated at runtime, so this `spi` module carries no runtime
dependency on `driver`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from agentpilot.spi import hashing
from agentpilot.spi.snapshot import BoundingBox

if TYPE_CHECKING:
    from agentpilot.driver.dom_fusion import LayoutInfo


class NodeType(IntEnum):
    """DOM node types (subset of the DOM spec that the pipeline distinguishes)."""

    ELEMENT_NODE = 1
    ATTRIBUTE_NODE = 2
    TEXT_NODE = 3
    CDATA_SECTION_NODE = 4
    PROCESSING_INSTRUCTION_NODE = 7
    COMMENT_NODE = 8
    DOCUMENT_NODE = 9
    DOCUMENT_TYPE_NODE = 10
    DOCUMENT_FRAGMENT_NODE = 11


@dataclass(slots=True)
class EnhancedAXNode:
    """Accessibility data merged onto a DOM node by `backendNodeId`."""

    role: str | None = None
    name: str | None = None
    description: str | None = None
    properties: dict[str, str | bool] = field(default_factory=dict)
    """Flattened AX property name -> value (e.g. ``{"checked": True,
    "expanded": False}``). Only the properties the interactivity/diff logic
    reads are kept."""


@dataclass(slots=True, eq=False)
class EnhancedDOMTreeNode:
    """A fused DOM/AX/Snapshot node. `eq=False` -> identity equality, so the
    self-referential `parent_node`/`children_nodes` graph never triggers
    recursive structural comparison; structural identity is expressed through
    the explicit `element_hash`/`stable_hash` methods instead."""

    node_id: int
    backend_node_id: int
    node_type: NodeType
    node_name: str
    node_value: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    # Layout / visibility (filled from the snapshot + frame walk).
    is_visible: bool | None = None
    is_scrollable: bool | None = None
    absolute_position: BoundingBox | None = None

    # Frame identity -- part of the cross-step identity for multi-frame pages.
    target_id: str | None = None
    frame_id: str | None = None
    session_id: str | None = None
    content_document: EnhancedDOMTreeNode | None = None

    # Shadow DOM.
    shadow_root_type: str | None = None  # "open" | "closed" | None
    shadow_roots: list[EnhancedDOMTreeNode] = field(default_factory=list)

    # Navigation (parent is back-reference; excluded from any traversal that
    # serializes the tree to avoid cycles).
    parent_node: EnhancedDOMTreeNode | None = None
    children_nodes: list[EnhancedDOMTreeNode] = field(default_factory=list)

    # Enrichment.
    ax_node: EnhancedAXNode | None = None
    snapshot: LayoutInfo | None = None
    has_js_click_listener: bool = False

    # ------------------------------------------------------------------ views

    @property
    def tag_name(self) -> str:
        return self.node_name.lower()

    @property
    def children(self) -> list[EnhancedDOMTreeNode]:
        return self.children_nodes

    @property
    def children_and_shadow_roots(self) -> list[EnhancedDOMTreeNode]:
        """Children plus shadow roots -- the full descendant set to traverse.
        Returns a fresh list so callers never mutate `children_nodes`."""

        if not self.shadow_roots:
            return list(self.children_nodes)
        return [*self.children_nodes, *self.shadow_roots]

    @property
    def ax_name(self) -> str:
        return (self.ax_node.name or "") if self.ax_node else ""

    @property
    def ax_role(self) -> str:
        return (self.ax_node.role or "") if self.ax_node else ""

    # -------------------------------------------------------------- identity

    def parent_branch_path(self) -> list[str]:
        """Tag-name chain from the document root down to this node (element
        nodes only). The structural half of the identity hash."""

        chain: list[EnhancedDOMTreeNode] = []
        current: EnhancedDOMTreeNode | None = self
        while current is not None:
            if current.node_type == NodeType.ELEMENT_NODE:
                chain.append(current)
            current = current.parent_node
        chain.reverse()
        return [node.node_name.lower() for node in chain]

    def element_hash(self) -> int:
        """EXACT structural identity (all static attributes)."""

        return hashing.element_hash(self.parent_branch_path(), self.attributes, self.ax_name)

    def stable_hash(self) -> int:
        """STABLE identity -- dynamic CSS-state classes filtered out. Preferred
        diff/replay key (survives re-render class churn)."""

        return hashing.stable_hash(self.parent_branch_path(), self.attributes, self.ax_name)

    def parent_branch_hash(self) -> int:
        """Ancestor-path-only identity; a MOVED element keeps its
        `element_hash` but changes this."""

        return hashing.parent_branch_hash(self.parent_branch_path())

    def xpath(self) -> str:
        """XPath for this node, stopping at iframe boundaries and passing
        through shadow roots (ported from browser-use). Used as the XPATH tier
        of the history-replay cascade."""

        segments: list[str] = []
        current: EnhancedDOMTreeNode | None = self
        while current is not None and current.node_type in (
            NodeType.ELEMENT_NODE,
            NodeType.DOCUMENT_FRAGMENT_NODE,
        ):
            if current.node_type == NodeType.DOCUMENT_FRAGMENT_NODE:
                current = current.parent_node  # pass through a shadow root
                continue
            if (
                current.parent_node is not None
                and current.parent_node.node_name.lower() == "iframe"
            ):
                break
            position = _sibling_position(current)
            index = f"[{position}]" if position > 0 else ""
            segments.insert(0, f"{current.node_name.lower()}{index}")
            current = current.parent_node
        return "/".join(segments)

    # -------------------------------------------------------------- text

    def all_text(self, max_depth: int = -1) -> str:
        """Concatenated descendant text -- used for accessible-name fallback and
        informational rendering."""

        parts: list[str] = []

        def collect(node: EnhancedDOMTreeNode, depth: int) -> None:
            if max_depth != -1 and depth > max_depth:
                return
            if node.node_type == NodeType.TEXT_NODE:
                parts.append(node.node_value)
            elif node.node_type == NodeType.ELEMENT_NODE:
                for child in node.children:
                    collect(child, depth + 1)

        collect(self, 0)
        return "\n".join(parts).strip()


def _sibling_position(element: EnhancedDOMTreeNode) -> int:
    """1-based index among same-tag element siblings, or 0 when it's the only
    one of its tag (XPath omits the predicate in that case)."""

    parent = element.parent_node
    if parent is None or not parent.children_nodes:
        return 0
    same_tag = [
        child
        for child in parent.children_nodes
        if child.node_type == NodeType.ELEMENT_NODE
        and child.node_name.lower() == element.node_name.lower()
    ]
    if len(same_tag) <= 1:
        return 0
    try:
        return same_tag.index(element) + 1
    except ValueError:
        return 0


# The model-facing index -> node map the serializer produces and ref-resolution
# consumes. Index is the element's `backendNodeId` (collision-resolved).
DOMSelectorMap = dict[int, EnhancedDOMTreeNode]
