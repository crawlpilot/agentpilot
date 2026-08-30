"""Interactivity classification for a fused DOM node -- ported from browser-use's
`ClickableElementDetector.is_interactive` (`dom/serializer/clickable_elements.py`)
and Browser4's `ClickableElementDetector`, adapted to
`agentpilot.spi.dom_tree.EnhancedDOMTreeNode`.

First-match-wins ladder, roughly strongest→weakest signal:
JS click listener → large iframe → label/span wrapping a control →
search-indicator classes/ids/data-attrs → AX state properties → native
interactive tags → interactive attributes/roles → icon-sized-with-affordance →
`cursor: pointer` / Chrome's own `isClickable`.

**Stealth note:** `has_js_click_listener` is the only signal that ever required
the CDP `Runtime` domain. The gating lives *upstream*: under the UI-driven
`no_runtime` mode the fusion engine never calls `getEventListeners`, so the flag
is simply `False` here and this module stays pure and Runtime-free. Every other
branch derives from the DOM/Snapshot/AX trees (all non-Runtime).
"""

from __future__ import annotations

from agentpilot.spi.dom_tree import EnhancedDOMTreeNode, NodeType

_NON_INTERACTIVE_TAGS = frozenset({"html", "body"})

_INTERACTIVE_TAGS = frozenset(
    {"button", "input", "select", "textarea", "a", "details", "summary", "option", "optgroup"}
)

_FORM_CONTROL_TAGS = frozenset({"input", "select", "textarea"})

_INTERACTIVE_ATTRIBUTES = frozenset(
    {"onclick", "onmousedown", "onmouseup", "onkeydown", "onkeyup", "tabindex"}
)

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
    if node.snapshot is not None:
        if node.snapshot.cursor_style == "pointer":
            return True
        if node.snapshot.is_clickable:
            return True

    return False
