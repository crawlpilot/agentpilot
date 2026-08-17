"""ref -> `Locator` resolution for the fusion perception engine.

Refs are `e<backendNodeId>` strings issued by the CDP DOM/Snapshot/Accessibility
fusion capture (`driver.dom_fusion_engine`). Each is resolved through the
`MatchLevel` selector cascade (id -> data-testid -> xpath -> role+name -> attr,
see `driver.fused_locators`), all via Playwright's own selector engine so no CDP
`Runtime` call is needed. The first candidate that resolves to exactly one live
element wins; a candidate matching multiple or zero elements is skipped
(ambiguous), so a click never lands on a lookalike.

Refs are epoch-scoped: `reset()` is called on every `SnapshotAction`, clearing
the whole index (fusion refs are backendNodeIds keyed only to the tree just
captured, and a superseded backendNodeId must not resolve against the new DOM).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from patchright.async_api import Locator, Page

from agentpilot.driver.fused_locators import candidate_selectors
from agentpilot.spi.errors import StaleRefError

if TYPE_CHECKING:
    from agentpilot.spi.dom_tree import EnhancedDOMTreeNode


@dataclass
class RefCache:
    """One instance per live `ContextRef`. `reset()` is called on every
    `SnapshotAction` -- refs are epoch-scoped, so the whole cache (not just
    individual entries) is invalidated on a new snapshot, matching the plan's
    "cache ref -> Locator per ContextRef, invalidated on next navigate/snapshot".
    """

    epoch: int = 0
    _locators: dict[str, Locator] = field(default_factory=dict)
    _fused: dict[str, EnhancedDOMTreeNode] = field(default_factory=dict)

    def reset(self, epoch: int) -> None:
        """Bumps the epoch and drops the resolved `Locator`s (page-bound, must
        be re-resolved) and the fused index (backendNodeIds are keyed only to
        the tree just captured; a superseded id must not resolve against the
        new DOM)."""

        self.epoch = epoch
        self._locators.clear()
        self._fused.clear()

    def record_fused(self, root: EnhancedDOMTreeNode) -> None:
        """Index a fused tree so `e<backendNodeId>` refs resolve. Walks children,
        shadow roots, and iframe content documents (never parent back-refs)."""

        from agentpilot.spi.dom_tree import NodeType

        self._fused.clear()
        stack = [root]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if node.node_type == NodeType.ELEMENT_NODE:
                self._fused[f"e{node.backend_node_id}"] = node
            if node.content_document is not None:
                stack.append(node.content_document)
            stack.extend(node.children_and_shadow_roots)

    async def resolve(self, page: Page, ref: str) -> Locator:
        # `e<backendNodeId>` refs resolve through the MatchLevel selector
        # cascade (id -> data-testid -> xpath -> role+name -> attr), all via
        # Playwright's selector engine (no CDP Runtime).
        node = self._fused.get(ref)
        if node is None:
            raise StaleRefError(ref, epoch_superseded=False)
        return await self._resolve_fused(page, ref, node)

    async def _resolve_fused(self, page: Page, ref: str, node: EnhancedDOMTreeNode) -> Locator:
        """Try each MatchLevel candidate; first that resolves to exactly one
        live element wins. A candidate matching multiple or zero elements is
        skipped (ambiguous), so a click never lands on a lookalike."""

        cached = self._locators.get(ref)
        if cached is not None:
            return cached

        for kind, value in candidate_selectors(node):
            try:
                if kind == "role":
                    role, name = value.split("\x1f", 1)
                    locator = page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
                else:
                    locator = page.locator(value)
                if await locator.count() == 1:
                    self._locators[ref] = locator
                    return locator
            except Exception:
                continue  # malformed selector / detached -> try the next tier

        raise StaleRefError(ref, epoch_superseded=False)
