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

`LayoutInfo` is defined here rather than in `driver.dom_fusion` (which produces
it) because it is pure data over `spi.geometry.BoundingBox` with no engine
behaviour, and `EnhancedDOMTreeNode.snapshot` -- an `spi` field -- is typed by
it. Defining it in the driver forced `spi` to import `driver` even under
`TYPE_CHECKING`, which import-linter counts as a layering violation (it reads
the AST, not the runtime graph). `driver.dom_fusion` re-exports the name, so
every existing `from crawlpilot.driver.dom_fusion import LayoutInfo` still works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from crawlpilot.spi import hashing
from crawlpilot.spi.geometry import BoundingBox


@dataclass
class LayoutInfo:
    """Per-node layout/paint data extracted from a DOMSnapshot document.

    Produced by `driver.dom_fusion.build_snapshot_lookup`; consumed by
    `EnhancedDOMTreeNode.snapshot` below and by `dom.clickable_elements` /
    `dom.paint_order`.
    """

    bounds: BoundingBox | None = None
    computed_styles: dict[str, str] = field(default_factory=dict)
    paint_order: int | None = None
    is_clickable: bool = False
    cursor_style: str | None = None
    client_rects: BoundingBox | None = None
    scroll_rects: BoundingBox | None = None


@dataclass(frozen=True)
class SnapshotView:
    """Which of the addressable elements the model is actually *offered*.

    The codebase already draws this line -- "addressable is what the driver can
    act on; offered is the narrower set the serializer decides to show a model"
    (`tests/browser/test_toolchain.py`) -- and every field here is on the offered
    side. Nothing below prunes the fused tree, so a ref the model was not shown
    still resolves if a caller names it; the filters change the *view*, not what
    exists.

    That is also why they are applied after paint-order and containment rather
    than before: occlusion is computed from the whole tree, and a modal scrim
    dropped by a role filter would stop hiding the buttons behind it.

    `SnapshotAction` has carried `viewport_only`, `max_nodes` and `roles` on the
    HTTP boundary since P1 and **nothing ever read them** -- the driver returns a
    tree and the caller serializes it, so the options had no route from the one
    to the other. This type is that route.
    """

    scope: frozenset[int] | None = None
    """Backend node ids of the subtree a CSS `selector` matched, resolved by the
    driver (it needs `DOM.querySelector`; this module stays pure). `None` means
    the whole document."""
    roles: tuple[str, ...] | None = None
    """Keep only these accessibility roles."""
    viewport: BoundingBox | None = None
    """Keep only elements intersecting this rectangle -- `viewport_only`,
    resolved by the driver from the live viewport size."""
    max_nodes: int | None = None
    """Cap on how many interactive elements are offered, in document order."""
    depth: int | None = None
    """Maximum indentation depth to render.

    agent-browser's other rendering flag, `compact` ("remove empty structural
    elements"), has no counterpart here because it describes what this renderer
    already does unconditionally: a node that emits no line contributes no
    indentation either (`render.render_tree`), so an empty wrapper is already
    invisible. A flag for it would toggle nothing.
    """

    @property
    def filters_offered_set(self) -> bool:
        """Whether anything here narrows *which elements* are offered, as opposed
        to only how they are rendered."""

        return (
            self.scope is not None
            or self.roles is not None
            or self.viewport is not None
            or self.max_nodes is not None
        )


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
    value: str | None = None
    """The node's live AX value -- what a textbox currently contains, what a
    slider is set to.

    Chrome puts this in the AX node's own `value` field, *not* among its
    `properties`, which is why reading properties alone missed it. Without it the
    only value available was the `value` **attribute**, and typing into a field
    never updates that -- so a filled input looked unchanged to anything
    comparing two captures."""
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

    selector_index: int | None = None
    """The model-facing ref (`e<selector_index>`), minted once by
    `assign_selector_indices` after the whole multi-target tree is merged.

    Normally the node's own `backend_node_id`, so refs stay recognisable in logs
    and traces. It has to be a separate field because `backendNodeId` is unique
    per *renderer*, not per browser: once cross-origin iframes are captured from
    their own targets, two unrelated elements can carry the same one, and one
    would silently shadow the other in the ref index. Colliding nodes get a
    synthetic index above every real id instead (browser-use's
    `_allocate_selector_index`, `dom/serializer/serializer.py:647-656`)."""

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
# consumes. Keyed by `selector_index`, assigned by `assign_selector_indices`.
DOMSelectorMap = dict[int, EnhancedDOMTreeNode]


def iter_elements(root: EnhancedDOMTreeNode):
    """Every element node at or under `root`, in document order, descending
    shadow roots and iframe content documents. Never follows `parent_node`, so
    the walk terminates on the self-referential graph."""

    stack = [root]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.node_type == NodeType.ELEMENT_NODE:
            yield node
        children = list(node.children_and_shadow_roots)
        if node.content_document is not None:
            children.append(node.content_document)
        stack.extend(reversed(children))


def assign_selector_indices(root: EnhancedDOMTreeNode) -> None:
    """Stamp a collision-free `selector_index` on every element in the merged tree.

    Run once, after every target's subtree has been stitched in -- the whole
    point is to see all the `backendNodeId`s together, and a per-target pass
    could not. A node keeps its own `backend_node_id` where that is unambiguous,
    so a ref still reads back to a real CDP id; a duplicate (two renderers
    numbering their nodes independently) gets a synthetic index above every real
    id, which cannot then collide with one.

    Ported from browser-use's `_allocate_selector_index`
    (`dom/serializer/serializer.py:647-656`). crawlpilot previously used the raw
    `backend_node_id` and described it as "collision-resolved" without resolving
    anything -- correct while only one target was ever captured, and a
    silently-wrong ref the moment cross-origin iframes were.
    """

    elements = list(iter_elements(root))
    reserved = {node.backend_node_id for node in elements}
    next_synthetic = max(reserved, default=0) + 1

    taken: set[int] = set()
    for node in elements:
        index = node.backend_node_id
        if index in taken:
            while next_synthetic in reserved or next_synthetic in taken:
                next_synthetic += 1
            index = next_synthetic
        taken.add(index)
        node.selector_index = index
