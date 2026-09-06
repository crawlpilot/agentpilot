"""Fused-tree matching for `ax_role` and `text` locators.

Two things differ from v1's `recipe/tree.py`, and both fix a documented v1
limitation rather than adding capability for its own sake:

1. **Attributes are readable.** v1's evaluator returned `matches[0].ax_name`
   unconditionally, so an `ax_role` locator could never read an `href` -- the
   accessible name was the only thing it could produce. The fused node carries
   its DOM attributes, so v2 reads them, with `attribute="text"` preserving the
   old behaviour as the default.

2. **`within` actually scopes.** v1's `name_in` matched the whole tree, which
   its own docstring flagged as an accepted false-positive risk: a same-named
   element elsewhere on the page could be picked up instead of the intended
   option. That is not hypothetical on a page whose size guide renders `XS`,
   `S`, `M` inside a drawer while the page behind it renders the same labels.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from agentpilot.recipe.v2.models import Locator
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode, NodeType


def node_ref(node: EnhancedDOMTreeNode) -> str:
    return f"e{node.backend_node_id}"


def _is_element(node: EnhancedDOMTreeNode) -> bool:
    return node.node_type == NodeType.ELEMENT_NODE


def _iter_elements(node: EnhancedDOMTreeNode) -> Iterator[EnhancedDOMTreeNode]:
    if _is_element(node):
        yield node
    for child in node.children_and_shadow_roots:
        yield from _iter_elements(child)


def node_text(node: EnhancedDOMTreeNode) -> str:
    """The node's visible text: its accessible name when it has one, otherwise
    the concatenation of its descendant text nodes."""

    if node.ax_name:
        return str(node.ax_name)
    parts: list[str] = []
    _collect_text(node, parts)
    return " ".join(p for p in parts if p).strip()


def _collect_text(node: EnhancedDOMTreeNode, out: list[str]) -> None:
    if node.node_type == NodeType.TEXT_NODE and node.node_value:
        out.append(str(node.node_value).strip())
    for child in node.children_and_shadow_roots:
        _collect_text(child, out)


def node_attribute(node: EnhancedDOMTreeNode, attribute: str) -> str | None:
    if attribute in ("text", "visible_text"):
        value = node_text(node)
        return value or None
    attrs = getattr(node, "attributes", None) or {}
    got = attrs.get(attribute)
    return str(got) if got is not None else None


def _name_matches(name: str, locator: Locator) -> bool:
    if locator.name_contains and locator.name_contains.lower() not in name.lower():
        return False
    if locator.name_in is not None and name not in locator.name_in:
        return False
    if locator.name_regex:
        try:
            if re.search(locator.name_regex, name) is None:
                return False
        except re.error:
            return False
    return True


def _scope(root: EnhancedDOMTreeNode, locator: Locator) -> EnhancedDOMTreeNode | None:
    """Narrow to `within`'s first match before matching, so an enumerated name
    set means "these N siblings" rather than "anything on the page called
    that"."""

    if locator.within is None:
        return root
    inner = find_nodes(root, locator.within)
    return inner[0] if inner else None


def find_nodes(root: EnhancedDOMTreeNode, locator: Locator) -> list[EnhancedDOMTreeNode]:
    """Every element matching `locator`, in document order.

    Only `ax_role` and `text` locators are tree-resolvable; css/xpath go
    through the page's own engines in `evaluate.py`, and a `within` written in
    css cannot be honoured here -- it is reported as no match rather than
    silently ignored, because silently widening a scope is how the v1
    false-positive happened.
    """

    if locator.kind not in ("ax_role", "text"):
        return []
    if locator.within is not None and locator.within.kind not in ("ax_role", "text"):
        return []

    scoped = _scope(root, locator)
    if scoped is None:
        return []

    matches: list[EnhancedDOMTreeNode] = []
    for node in _iter_elements(scoped):
        if locator.kind == "ax_role":
            if not node.ax_role or node.ax_role != locator.role:
                continue
            name = str(node.ax_name or "")
            if not name and (locator.name_contains or locator.name_in or locator.name_regex):
                continue
            if _name_matches(name, locator):
                matches.append(node)
        else:  # text
            wanted = locator.text or ""
            if wanted and wanted.lower() in node_text(node).lower():
                matches.append(node)
    return matches
