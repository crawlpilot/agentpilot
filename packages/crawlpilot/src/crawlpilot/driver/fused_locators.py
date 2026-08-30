"""Derive Playwright locator candidates for a fused `EnhancedDOMTreeNode`, in
descending confidence -- the `RefCache` fusion tier and the cross-step
history-replay cascade (browser-use `MatchLevel`) both consume this.

Pure and driver-free: given a node, return an ordered list of `(kind, value)`
candidates the resolver tries in turn (first that resolves to exactly one live
element wins). Order mirrors `MatchLevel`: a unique `id` / `data-testid`
(EXACT-ish) → frame-local xpath (XPATH) → role + accessible name (AX_NAME) →
other identifying attributes (ATTRIBUTE).

Stealth-safe: every candidate resolves through Playwright's own selector engine
(CSS / xpath / role), never a CDP `Runtime`/`DOM.resolveNode` round trip, so it
works under the `no_runtime` stealth mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentpilot.spi.dom_tree import EnhancedDOMTreeNode

# Locator kinds the resolver understands.
#   "css"   -> page.locator(value)
#   "xpath" -> page.locator(value)  (value already prefixed with "xpath=")
#   "role"  -> page.get_by_role(role, name=name, exact=True); value = "role\x1fname"
LocatorCandidate = tuple[str, str]


def _attr_selector(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{name}="{escaped}"]'


def candidate_selectors(node: EnhancedDOMTreeNode) -> list[LocatorCandidate]:
    """Ordered locator candidates for `node`, most-specific first."""

    candidates: list[LocatorCandidate] = []
    attrs = node.attributes

    element_id = attrs.get("id")
    if element_id:
        candidates.append(("css", _attr_selector("id", element_id)))

    testid = attrs.get("data-testid")
    if testid:
        candidates.append(("css", _attr_selector("data-testid", testid)))

    xpath = node.xpath()
    if xpath:
        # node.xpath() is frame-local without a leading slash; make it absolute.
        candidates.append(("xpath", f"xpath=/{xpath}"))

    if node.ax_role and node.ax_name:
        candidates.append(("role", f"{node.ax_role}\x1f{node.ax_name}"))

    for attr in ("name", "aria-label", "placeholder"):
        value = attrs.get(attr)
        if value:
            candidates.append(("css", _attr_selector(attr, value)))

    return candidates
