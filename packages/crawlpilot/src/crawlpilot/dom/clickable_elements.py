"""Interactivity classification for a fused DOM node -- ported from browser-use's
`ClickableElementDetector.is_interactive` (`dom/serializer/clickable_elements.py`)
and Browser4's `ClickableElementDetector`, adapted to
`crawlpilot.spi.dom_tree.EnhancedDOMTreeNode`.

First-match-wins ladder, roughly strongest→weakest signal:
JS click listener → large iframe → label/span wrapping a control →
search-indicator classes/ids/data-attrs → AX state properties → native
interactive tags → interactive attributes → non-negative `tabindex` →
`contenteditable` → interactive roles → icon-sized-with-affordance →
*non-inherited* `cursor: pointer` / Chrome's own `isClickable`.

**Stealth note:** `has_js_click_listener` is the only signal that ever required
the CDP `Runtime` domain. The gating lives *upstream*: under the UI-driven
`no_runtime` mode the fusion engine never calls `getEventListeners`, so the flag
is simply `False` here and this module stays pure and Runtime-free. Every other
branch derives from the DOM/Snapshot/AX trees (all non-Runtime).
"""

from __future__ import annotations

from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode, NodeType

_NON_INTERACTIVE_TAGS = frozenset({"html", "body"})

_INTERACTIVE_TAGS = frozenset(
    {"button", "input", "select", "textarea", "a", "details", "summary", "option", "optgroup"}
)

_FORM_CONTROL_TAGS = frozenset({"input", "select", "textarea"})

_INTERACTIVE_ATTRIBUTES = frozenset(
    {"onclick", "onmousedown", "onmouseup", "onkeydown", "onkeyup"}
)
"""Attributes interactive by their mere presence.

`tabindex` is deliberately *not* here: it is interactive by its **value**, and
`tabindex="-1"` means the opposite -- explicitly removed from tab order, which
authors use to make a container programmatically focusable without offering it
to the user. Matching on presence marked every such wrapper interactive.
Handled by `_tabindex_is_interactive` instead (agent-browser's
`snapshot.rs::find_cursor_interactive_elements`: `tabIndex !== null &&
tabIndex !== '-1'`)."""

_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "menuitem",
        "option",
        "radio",
        "checkbox",
        "tab",
        "textbox",
        "combobox",
        "slider",
        "spinbutton",
        "listbox",
        "search",
        "searchbox",
        "row",
        "cell",
        "gridcell",
    }
)

_SEARCH_INDICATORS = frozenset(
    {
        "search",
        "magnify",
        "glass",
        "lookup",
        "find",
        "query",
        "search-icon",
        "search-btn",
        "search-button",
        "searchbox",
    }
)

# AX properties whose mere *presence* implies an interactive widget.
_PRESENCE_INTERACTIVE_PROPS = frozenset({"checked", "expanded", "pressed", "selected"})
# AX properties interactive only when truthy.
_TRUTHY_INTERACTIVE_PROPS = frozenset(
    {"focusable", "editable", "settable", "required", "autocomplete", "keyshortcuts"}
)


def _has_form_control_descendant(node: EnhancedDOMTreeNode, max_depth: int = 2) -> bool:
    """Detect a nested form control within `max_depth` (handles label > span >
    input wrappers common in component libraries)."""

    if max_depth <= 0:
        return False
    for child in node.children_and_shadow_roots:
        if child.node_type != NodeType.ELEMENT_NODE:
            continue
        if child.tag_name in _FORM_CONTROL_TAGS:
            return True
        if _has_form_control_descendant(child, max_depth=max_depth - 1):
            return True
    return False


def _matches_search_indicator(node: EnhancedDOMTreeNode) -> bool:
    classes = node.attributes.get("class", "").lower()
    if any(ind in classes for ind in _SEARCH_INDICATORS):
        return True
    element_id = node.attributes.get("id", "").lower()
    if any(ind in element_id for ind in _SEARCH_INDICATORS):
        return True
    for attr_name, attr_value in node.attributes.items():
        if attr_name.startswith("data-") and any(
            ind in attr_value.lower() for ind in _SEARCH_INDICATORS
        ):
            return True
    return False


def _tabindex_is_interactive(node: EnhancedDOMTreeNode) -> bool:
    """`tabindex` by value, not by presence. `-1` is explicitly *not* tab
    reachable, and an unparseable value is treated the same as absent."""

    raw = node.attributes.get("tabindex")
    if raw is None:
        return False
    try:
        return int(raw.strip()) >= 0
    except ValueError:
        return False


def _is_contenteditable(node: EnhancedDOMTreeNode) -> bool:
    """A rich-text surface is a text input by another name.

    HTML spec spelling: the attribute is an enumerated one, so `""` and
    `"true"` enable editing while `"false"` disables it -- and `contenteditable`
    written bare parses as `""`. `"inherit"` (and any other value) leaves the
    decision to an ancestor, which will be classified on its own merits.
    """

    value = node.attributes.get("contenteditable")
    return value is not None and value.strip().lower() in {"", "true"}


def _inherits_pointer_cursor(node: EnhancedDOMTreeNode) -> bool:
    """Whether this node's `cursor: pointer` is merely inherited from its parent.

    `cursor` inherits, so a single `cursor: pointer` on a card container gives
    every descendant -- every div, span and image inside it -- the same computed
    style. Treating that as an interactivity signal turns one clickable card into
    dozens of refs, which crowds genuinely distinct controls out of the
    serializer's token budget and gives the model many ways to express one
    action.

    agent-browser applies the same rule in
    `snapshot.rs::find_cursor_interactive_elements` ("Skip elements that only
    inherit cursor:pointer from an ancestor"). The parent keeps its ref, so the
    card stays clickable; only the redundant descendants are dropped.
    """

    parent = node.parent_node
    return parent is not None and parent.snapshot is not None and (
        parent.snapshot.cursor_style == "pointer"
    )


def _ax_property_verdict(node: EnhancedDOMTreeNode) -> bool | None:
    """Interactivity from AX state. Returns True/False for a decisive verdict,
    or None to fall through to later heuristics. `disabled`/`hidden` are hard
    rejects; the state properties are interactive signals."""

    if node.ax_node is None or not node.ax_node.properties:
        return None
    props = node.ax_node.properties
    if props.get("disabled") or props.get("hidden"):
        return False
    if any(key in props for key in _PRESENCE_INTERACTIVE_PROPS):
        return True
    if any(props.get(key) for key in _TRUTHY_INTERACTIVE_PROPS):
        return True
    return None


def is_interactive(node: EnhancedDOMTreeNode) -> bool:
    """Whether the agent should be able to act on this node. See module docstring
    for the ordered ladder."""

    if node.node_type != NodeType.ELEMENT_NODE:
        return False

    tag = node.tag_name
    if tag in _NON_INTERACTIVE_TAGS:
        return False

    # Framework click handlers (React onClick / Vue @click / Angular (click)),
    # detected via CDP getEventListeners -- only ever set when Runtime is allowed.
    if node.has_js_click_listener:
        return True

    # Large iframes are treated as interactive (scrollable) surfaces.
    if tag in {"iframe", "frame"}:
        bounds = node.snapshot.bounds if node.snapshot else None
        if bounds is not None and bounds.width > 100 and bounds.height > 100:
            return True

    # label/span component wrappers around real controls.
    if tag == "label":
        if node.attributes.get("for"):
            return False  # proxies to an external input; don't double-activate
        if _has_form_control_descendant(node):
            return True
    elif tag == "span" and _has_form_control_descendant(node):
        return True

    if _matches_search_indicator(node):
        return True

    verdict = _ax_property_verdict(node)
    if verdict is not None:
        return verdict

    if tag in _INTERACTIVE_TAGS:
        return True

    if any(attr in node.attributes for attr in _INTERACTIVE_ATTRIBUTES):
        return True
    if _tabindex_is_interactive(node):
        return True
    if _is_contenteditable(node):
        return True
    if node.attributes.get("role") in _INTERACTIVE_ROLES:
        return True
    if node.ax_role in _INTERACTIVE_ROLES:
        return True

    # Icon-sized element carrying an affordance attribute.
    bounds = node.snapshot.bounds if node.snapshot else None
    if bounds is not None and 10 <= bounds.width <= 50 and 10 <= bounds.height <= 50:
        if any(
            attr in node.attributes
            for attr in ("class", "role", "onclick", "data-action", "aria-label")
        ):
            return True

    # Weakest signals: an explicit pointer cursor, or Chrome's own clickability
    # hint from DOMSnapshot (both Runtime-free).
    #
    # Reached only when every stronger signal above declined, so a node here has
    # nothing going for it but its cursor -- which is exactly the case where an
    # inherited `pointer` is meaningless. A node with a real affordance
    # (onclick, role, tabindex, a form control inside it) returned True long
    # before this point and keeps its ref regardless of what its parent's cursor
    # computes to.
    if node.snapshot is not None:
        if node.snapshot.cursor_style == "pointer" and not _inherits_pointer_cursor(node):
            return True
        if node.snapshot.is_clickable:
            return True

    return False
