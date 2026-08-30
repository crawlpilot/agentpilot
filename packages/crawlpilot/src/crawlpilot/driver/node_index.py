"""ref -> fused node lookup. Replaces the selector cascade in `driver/ref_cache.py`.

Refs are `e<index>` strings minted by the CDP DOM/Snapshot/Accessibility fusion
capture (`driver.dom_fusion_engine`). A ref resolves to the captured
`EnhancedDOMTreeNode` by dictionary lookup and **nothing else** -- no CSS, no
XPath, no Playwright selector engine. The driver then acts on that node's
`(session_id, backend_node_id)` directly over CDP.

This is browser-use's model (`browser/session.py:2451-2466`: "pure in-memory
dict lookup, zero CDP"), and the reason it holds up where the previous design
did not. `ref_cache.RefCache` tried to *re-derive* a selector for an element it
had already captured -- id, then test id, then xpath, then role+name -- and
accepted a candidate only when `await locator.count() == 1`. Three failure modes
followed, none of which browser-use can have:

- **Ambiguity.** Duplicate `id`s and repeated `name`/`aria-label` are ordinary on
  real pages, so every tier matched more than one element, every candidate was
  skipped as unsafe, and a perfectly addressable element raised `StaleRefError`.
- **Frames.** Iframe content documents were indexed but every candidate went to
  `page.locator()`, which searches the main frame only -- so an iframe ref could
  never resolve, or resolved to an unrelated main-frame lookalike and the click
  landed on the wrong element.
- **Shadow DOM.** `node.xpath()` passes *through* shadow roots, producing a path
  document XPath can never match.

Refs are epoch-scoped: `reset()` is called on every `SnapshotAction` and on every
navigation, clearing the index. A ref minted by a superseded capture must not
resolve against the new DOM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode


@dataclass
class NodeIndex:
    """One instance per live `ContextRef`: `ref -> EnhancedDOMTreeNode` for the
    most recent capture.

    Nothing is cached beyond the captured nodes themselves. browser-use
    deliberately re-resolves from the `backendNodeId` on every action rather than
    holding a CDP `objectId` or a Playwright handle, because a stored handle goes
    stale silently while a re-resolve fails loudly.
    """

    _nodes: dict[str, EnhancedDOMTreeNode] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, ref: str) -> bool:
        return ref in self._nodes

    def reset(self) -> None:
        """Drop the index. Called on every snapshot and every navigation --
        fusion refs are keyed only to the tree just captured."""

        self._nodes.clear()

    def record(self, root: EnhancedDOMTreeNode) -> None:
        """Index a fused tree so `e<index>` refs resolve.

        Walks children, shadow roots and iframe content documents (never parent
        back-refs). Every element node is addressable, not only the ones the
        serializer chose to show the model: the serializer's job is deciding what
        is worth *rendering*, and a caller that already holds a ref -- a replayed
        recipe, an extension -- should not be limited by that editorial choice.
        """

        from crawlpilot.spi.dom_tree import NodeType

        self._nodes.clear()
        stack = [root]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if node.node_type == NodeType.ELEMENT_NODE:
                self._nodes[f"e{node.selector_index}"] = node
            if node.content_document is not None:
                stack.append(node.content_document)
            stack.extend(node.children_and_shadow_roots)

    def get(self, ref: str) -> EnhancedDOMTreeNode | None:
        """The captured node behind `ref`, or `None` when no capture minted it."""

        return self._nodes.get(ref)
