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
A `SnapshotView` (defined in `spi.dom_tree`, because `ActionResult` carries one
and `spi` cannot import `dom`) narrows what the result *offers*.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import structlog

from crawlpilot.dom import render
from crawlpilot.dom.clickable_elements import is_interactive
from crawlpilot.dom.paint_order import PaintEntry, compute_occluded
from crawlpilot.spi.dom_tree import (
    DOMSelectorMap,
    EnhancedDOMTreeNode,
    NodeType,
    SnapshotView,
    iter_elements,
)
from crawlpilot.spi.geometry import BoundingBox

log = structlog.get_logger(__name__)

# Tags with no useful content for the agent -- pruned entirely.
_DISABLED_TAGS = frozenset(
    {"script", "style", "head", "meta", "link", "noscript", "title", "base", "template"}
)
_SVG_TAG = "svg"
_CONTAINMENT_THRESHOLD = 0.99
_FORM_CONTROL_TAGS = frozenset({"input", "select", "textarea", "option"})

# Site chrome, by landmark. Semantic and site-agnostic: a `<nav>` is chrome
# wherever it appears, where a class name would only be a guess.
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})
_CHROME_ROLES = frozenset(
    {"navigation", "banner", "contentinfo", "complementary", "search"}
)


def _is_chrome(node: SimplifiedNode) -> bool:
    original = node.original
    if original.node_type != NodeType.ELEMENT_NODE:
        return False
    if original.tag_name in _CHROME_TAGS:
        return True
    role = (original.ax_role or "").lower()
    if role in _CHROME_ROLES:
        return True
    return (original.attributes.get("role") or "").lower() in _CHROME_ROLES


def _prune_chrome(node: SimplifiedNode) -> SimplifiedNode | None:
    """The tree with navigation, header, footer and complementary panels gone.

    Subtractive rather than "keep `<main>`": plenty of pages mark neither, and
    a rule that keeps nothing when `main` is absent would be worse than no rule
    at all. See `SnapshotView.content_only`.
    """

    if _is_chrome(node):
        return None
    node.children = [
        kept for kept in (_prune_chrome(child) for child in node.children) if kept
    ]
    return node


@dataclass(frozen=True)
class PromotedControl:
    """The identity of a form control that exists in the DOM but not in the
    accessibility tree, projected onto the wrapper that stands in for it."""

    role: str
    """`"radio"` or `"checkbox"` -- taken from the hidden input's `type`."""
    checked: str | None
    """`"true"` / `"false"` / `"mixed"`, or None when unknown."""


def _promoted_control(node: EnhancedDOMTreeNode) -> PromotedControl | None:
    """The hidden radio/checkbox this node visually stands for, if any.

    The pattern is a component-library staple: a `<label>` (or styled `<div>`)
    wraps an `<input type=radio>` set to `display:none`, and CSS draws the
    selected state on the wrapper. Chrome excludes a `display:none` input from
    the accessibility tree entirely, so the fused node for the wrapper carries
    role `LabelText` (or nothing) and an empty name -- the model is shown an
    anonymous box and has no way to tell a selected option from an unselected
    one.

    Ported from agent-browser's `promote_hidden_inputs` (`snapshot.rs:914`),
    which detects the same shape in its cursor-interactivity scan.

    Only `display:none` / `visibility:hidden` / the `hidden` attribute count, and
    deliberately **not** `opacity:0` or an off-screen "sr-only" clip: those
    inputs stay in the accessibility tree and already surface with their own
    `role=radio`, so promoting the wrapper too would show the model the same
    control twice.
    """

    if node.tag_name in _FORM_CONTROL_TAGS:
        return None  # a real control speaks for itself
    control = _hidden_form_control(node)
    if control is None:
        return None
    kind = control.attributes.get("type", "").strip().lower()
    if kind not in ("radio", "checkbox"):
        return None
    return PromotedControl(role=kind, checked=_checked_state(control))


def _hidden_form_control(
    node: EnhancedDOMTreeNode, max_depth: int = 2
) -> EnhancedDOMTreeNode | None:
    """The nearest visually hidden `<input>` within `max_depth`. Mirrors the
    depth bound in `clickable_elements._has_form_control_descendant`, which is
    what marked this wrapper interactive in the first place."""

    if max_depth <= 0:
        return None
    for child in node.children_and_shadow_roots:
        if child.node_type != NodeType.ELEMENT_NODE:
            continue
        if child.tag_name == "input" and _is_visually_hidden(child):
            return child
        found = _hidden_form_control(child, max_depth=max_depth - 1)
        if found is not None:
            return found
    return None


def _is_visually_hidden(node: EnhancedDOMTreeNode) -> bool:
    if "hidden" in node.attributes:
        return True
    styles = node.snapshot.computed_styles if node.snapshot else None
    if not styles:
        return False
    return styles.get("display") == "none" or styles.get("visibility") == "hidden"


def _checked_state(control: EnhancedDOMTreeNode) -> str | None:
    """`"true"` / `"false"` / `"mixed"` for a checkbox or radio.

    `indeterminate` is a JS-only property with no attribute reflection, so it is
    only visible when the accessibility tree happens to carry the node. When it
    does not, absence of `checked` means unchecked -- which is the answer the
    model needs, and is right for every case except the rare tri-state box.
    """

    ax_properties = control.ax_node.properties if control.ax_node else None
    if ax_properties and "checked" in ax_properties:
        value = ax_properties["checked"]
        if isinstance(value, str):
            return value
        return "true" if value else "false"
    return "true" if "checked" in control.attributes else "false"


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
    excluded_by_view: bool = False
    """Addressable, but outside what this snapshot's `SnapshotView` offers.
    Kept in the tree -- it may still be an occluder, a text-bearing ancestor or
    a shadow host -- but it earns no ref."""
    is_shadow_host: bool = False
    promoted: PromotedControl | None = None
    """Set when this node stands in for a form control Chrome dropped from the
    accessibility tree -- see `_promoted_control`. The render prefers it over the
    node's own role, so the model sees `radio checked=false` rather than an
    unnamed wrapper."""

    def text_content(self) -> str:
        """Own text for a kept non-interactive node -- text-node value, else
        empty (structural nodes contribute only indentation/children)."""

        if self.original.node_type == NodeType.TEXT_NODE:
            return render.normalize_text(self.original.node_value)
        return ""


@dataclass
class SerializedDOM:
    selector_map: DOMSelectorMap
    llm_text: str
    rendered_indices: set[int] = field(default_factory=set)
    """The subset of `selector_map` the model can actually see -- equal to its
    keys unless `max_length` truncated the render. Callers pre-validating a
    model's chosen ref should check this, not `selector_map`."""


def _bounds(node: EnhancedDOMTreeNode) -> BoundingBox | None:
    if node.absolute_position is not None:
        return node.absolute_position
    return node.snapshot.bounds if node.snapshot else None


def _build_simplified(node: EnhancedDOMTreeNode) -> SimplifiedNode | None:
    """Recursively build the simplified subtree for `node`, or None if neither
    it nor any descendant is worth keeping."""

    if node.node_type == NodeType.TEXT_NODE:
        # Normalized, not merely stripped: a text node holding nothing but
        # zero-width characters is empty as far as a reader is concerned, and
        # keeping it costs the model an indentation level for an invisible line.
        text = render.normalize_text(node.node_value)
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
        promoted=_promoted_control(node) if interactive else None,
    )


def _iter_simplified(root: SimplifiedNode):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _apply_paint_order(
    root: SimplifiedNode, full: EnhancedDOMTreeNode, *, test_text: bool = False
) -> None:
    """Mark interactive nodes hidden behind something opaque painted later.

    Covers are gathered from `full`, the *original* tree, not from the
    simplified one. Two reasons, and the second is why this was broken: only
    interactive nodes were considered at all, and the simplified tree has
    already dropped every childless non-interactive element anyway. Between
    them, the commonest real occluder could never occlude anything -- a modal
    scrim, a cookie-banner backdrop and a loading overlay are all empty
    `<div>`s. The button underneath stayed in the model's list and clicking it
    did nothing, which is the worst kind of failure because it looks like it
    worked.

    Only interactive nodes are ever *marked*; everything else is here to cover.
    """

    entries: list[PaintEntry] = []
    seen: set[int] = set()

    def add(original: EnhancedDOMTreeNode, *, interactive: bool) -> None:
        if original.backend_node_id in seen:
            return
        bounds = _bounds(original)
        snapshot = original.snapshot
        if bounds is None or snapshot is None or snapshot.paint_order is None:
            return
        styles = snapshot.computed_styles or {}
        try:
            opacity = float(styles.get("opacity", "1"))
        except (ValueError, TypeError):
            opacity = 1.0
        bg = styles.get("background-color", "rgba(0, 0, 0, 0)")
        entry = PaintEntry(
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
        # An interactive node is here to be *tested*; anything else earns a place
        # only if it can actually cover something. Keeps the sweep to the nodes
        # that matter rather than every element on the page.
        if interactive or entry.is_opaque_cover():
            seen.add(original.backend_node_id)
            entries.append(entry)

    # Text is swept only when something will read the answer -- `_hide_unreadable_text`
    # is the sole consumer, and a page has far more text nodes than interactive
    # ones, so testing them unconditionally would cost every caller for a flag
    # most of them do not set.
    def wanted(node: SimplifiedNode) -> bool:
        if node.is_interactive:
            return True
        return test_text and node.original.node_type == NodeType.TEXT_NODE

    for node in _iter_simplified(root):
        if wanted(node):
            add(node.original, interactive=True)
    for element in iter_elements(full):
        add(element, interactive=False)

    occluded = compute_occluded(entries)
    for node in _iter_simplified(root):
        if wanted(node) and node.original.backend_node_id in occluded:
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
    # A `<button>` or `<a>` is a real control and speaks for itself, exactly as
    # `_FORM_CONTROL_TAGS` does -- it was simply missing from that set, which
    # covers only `input`/`select`/`textarea`/`option`.
    #
    # The gap is not theoretical. Sites wrap an accordion trigger in a clickable
    # container, the button then fills ~100% of its wrapper, and the dedup drops
    # the button in favour of the wrapper. The button has no ref from that point
    # on and NOTHING brings it back -- unlike the viewport gate, scrolling does
    # not help, which is what makes it so hard to diagnose from the outside. A
    # Zara build spent its last four steps on "the accordion button isn't
    # minting a ref", scrolled to it as instructed, and still got nothing.
    #
    # Deduping the wrapper away instead is not an option here: this pass only
    # ever marks the child, and the wrapper may be the thing that carries the
    # handler. Keeping both costs one line in the render and leaves the model
    # able to name the element it can actually see.
    if node.tag_name in ("button", "a"):
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


def _apply_view(root: SimplifiedNode, view: SnapshotView) -> None:
    """Mark interactive nodes the view does not offer.

    Marking rather than removing: an element outside the viewport is still a
    legitimate occluder, still an ancestor of text worth rendering, and still
    addressable by a caller who names its ref. Deleting it would quietly change
    all three.
    """

    if view.visible_text_only:
        _hide_unreadable_text(root)

    if not view.filters_offered_set:
        return

    offered = 0
    for node in _iter_document_order(root):
        if not node.is_interactive or node.ignored_by_paint_order or node.excluded_by_parent:
            continue
        if not _in_view(node, view):
            node.excluded_by_view = True
            continue
        # `max_nodes` is applied last and in document order, so the cap keeps the
        # elements nearest the top of the page -- the ones a reader reaches
        # first -- rather than an arbitrary subset.
        if view.max_nodes is not None and offered >= view.max_nodes:
            node.excluded_by_view = True
            continue
        offered += 1


def _hide_unreadable_text(root: SimplifiedNode) -> None:
    """Mark text nodes a person could not read, for `visible_text_only`.

    Three rules, all of them browser-use's (`DOMTreeSerializer.serialize_tree`),
    and each carries most of its weight on a different kind of page:

    - **Not visible.** A commerce page keeps its collapsed accordions, its
      offscreen carousel slides and its SEO copy in the DOM. On a product page
      that is the majority of the text by volume, and none of it is something an
      agent can click.
    - **Painted over.** Text under an open modal or a cookie wall reads exactly
      like text the agent can act on, and acting on it does nothing.
    - **One character.** Separators, bullets and stray whitespace glyphs, one
      render line each.

    `is_visible` is tri-state: `None` means the fusion could not say, and the
    honest reading of "unknown" is to keep the text. Only a definite `False`
    hides it.
    """

    for node in _iter_document_order(root):
        if node.original.node_type != NodeType.TEXT_NODE:
            continue
        if (
            node.original.is_visible is False
            or node.ignored_by_paint_order
            or len(render.normalize_text(node.original.node_value)) <= 1
        ):
            node.excluded_by_view = True


def _in_view(node: SimplifiedNode, view: SnapshotView) -> bool:
    original = node.original
    if view.scope is not None and original.backend_node_id not in view.scope:
        return False
    if view.roles is not None and original.ax_role not in view.roles:
        return False
    if view.viewport is not None:
        bounds = _bounds(original)
        # An element with no geometry at all -- a hidden file input, a checkbox
        # behind a styled label -- is kept: it is not *outside* the viewport, it
        # simply has no box to compare, and dropping it would make
        # `viewport_only` silently remove controls that are perfectly usable.
        if bounds is not None and not _intersects(bounds, view.viewport):
            return False
    return True


def _intersects(a: BoundingBox, b: BoundingBox) -> bool:
    return not (
        a.x + a.width <= b.x
        or b.x + b.width <= a.x
        or a.y + a.height <= b.y
        or b.y + b.height <= a.y
    )


def _iter_document_order(root: SimplifiedNode) -> Iterator[SimplifiedNode]:
    """Depth-first, children in order -- unlike `_iter_simplified`, whose stack
    reverses siblings. `max_nodes` and the render both depend on the order a
    reader would encounter, so it cannot be the arbitrary one."""

    yield root
    for child in root.children:
        yield from _iter_document_order(child)


def _assign_indices(root: SimplifiedNode, new_backend_ids: set[int]) -> DOMSelectorMap:
    """Copy the capture's `selector_index` onto each surviving interactive node
    and build the `selector_map`.

    The index is *read*, never computed here. It is minted once by
    `assign_selector_indices` when the fused tree is captured, so the driver's
    ref lookup and this map cannot disagree -- previously both independently used
    the raw `backend_node_id`, which agreed only for as long as a single target
    was ever captured.

    A tree assembled by hand rather than by a capture (tests, a replayed
    fixture) has no index; it falls back to `backend_node_id`, which is what
    `assign_selector_indices` would have chosen for it anyway absent a collision.
    """

    selector_map: DOMSelectorMap = {}
    # DEBUG: why a control the model can SEE in the page text cannot be named.
    # Four passes withhold a ref and from the outside all four look identical --
    # the element is simply absent -- but only the viewport one is fixed by
    # scrolling. An agent that cannot tell them apart re-queries instead, which
    # can never work (`find_elements` returns text, never refs), and burns its
    # whole step budget doing it.
    dropped: dict[str, list[str]] = {}
    for node in _iter_simplified(root):
        if node.is_interactive:
            why = (
                "occluded" if node.ignored_by_paint_order
                else "contained" if node.excluded_by_parent
                else "view" if node.excluded_by_view
                else ""
            )
            if why:
                name = node.original.ax_name or node.original.tag_name
                dropped.setdefault(why, []).append(str(name)[:40])
    for node in _iter_simplified(root):
        if (
            node.is_interactive
            and not node.ignored_by_paint_order
            and not node.excluded_by_parent
            and not node.excluded_by_view
        ):
            original = node.original
            index = (
                original.selector_index
                if original.selector_index is not None
                else original.backend_node_id
            )
            node.selector_index = index
            node.is_new = original.backend_node_id in new_backend_ids
            selector_map[index] = original
    if dropped:
        log.info(
            "serializer.controls_without_a_ref",
            minted=len(selector_map),
            **{why: names[:15] for why, names in dropped.items()},
        )
    return selector_map


def serialize(
    root: EnhancedDOMTreeNode,
    *,
    new_backend_ids: set[int] | None = None,
    include_attributes: tuple[str, ...] = render.DEFAULT_INCLUDE_ATTRIBUTES,
    max_length: int | None = None,
    view: SnapshotView | None = None,
) -> SerializedDOM:
    """Run the full pipeline and return the `selector_map` + rendered text.
    `new_backend_ids` (from the diff) drive the inline `*` new-element markers.
    `view` narrows what is offered -- see `SnapshotView`."""

    simplified = _build_simplified(root)
    if simplified is None:
        return SerializedDOM(selector_map={}, llm_text="(empty page)")

    view = view or SnapshotView()
    if view.content_only:
        # Before every other pass: occlusion and containment are cheaper over a
        # smaller tree, and nothing downstream wants to reason about chrome.
        pruned = _prune_chrome(simplified)
        # A page that is *all* chrome is a page this rule cannot help with --
        # keep what there is rather than hand back nothing.
        if pruned is not None and _iter_document_order(pruned):
            simplified = pruned
    _apply_paint_order(simplified, root, test_text=view.visible_text_only)
    _apply_containment(simplified)
    # After both, deliberately: occlusion and containment reason about the whole
    # page, and a node the view hides is still allowed to hide others.
    _apply_view(simplified, view)
    selector_map = _assign_indices(simplified, new_backend_ids or set())
    # A selector has to be written against structure, and the default whitelist
    # deliberately omits `class`. See `SnapshotView.for_authoring`.
    if view.for_authoring and include_attributes is render.DEFAULT_INCLUDE_ATTRIBUTES:
        include_attributes = render.AUTHORING_INCLUDE_ATTRIBUTES
    rendered = render.render_tree(
        simplified,
        include_attributes=include_attributes,
        max_length=max_length,
        depth=view.depth,
        for_authoring=view.for_authoring,
    )
    return SerializedDOM(
        selector_map=selector_map,
        llm_text=rendered.text or "(empty page)",
        rendered_indices=rendered.rendered_indices,
    )
