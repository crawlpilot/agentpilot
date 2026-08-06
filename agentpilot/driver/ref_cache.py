"""ref -> `Locator` resolution.

**EXACT tier**: Patchright/Playwright resolve `aria_snapshot(mode="ai")`
refs natively via `page.locator(f"aria-ref={ref}")` -- this is the
documented mechanism Playwright's own AI/MCP tooling uses to turn a
snapshot's `[ref=e3]` back into a live element; it is not a hand-rolled
hack, and the P0 plan's "spike: does Patchright's binary support this"
question resolves yes.

**AX_NAME fallback**: if the exact ref no longer resolves to exactly one
element (DOM mutated since the snapshot, but no navigation happened, so the
epoch is still current), fall back to role+accessible-name lookup via
`page.get_by_role(role, name=name, exact=True)`.

`plan.md`'s full cascade also specifies STABLE (attr hash minus dynamic
classes), XPATH (re-derived CSS + iframe-chain via `frame_locator()`), and
ATTRIBUTE tiers -- deliberately not built in this pass. Those tiers exist to
survive markup changes *within* an epoch beyond "the exact ref moved but the
role/name didn't"; EXACT+AX_NAME already covers the common case (a snapshot
followed promptly by an action) and is called out here as reduced scope
rather than silently dropped.

**Epoch enforcement**: a ref from a superseded epoch (i.e. a newer snapshot
has been taken since) is rejected before any resolution attempt at all --
no cascade, no lookalike click on stale DOM.
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
class _RefMeta:
    epoch: int
    role: str
    name: str


@dataclass
class RefCache:
    """One instance per live `ContextRef`. `reset()` is called on every
    `SnapshotAction` -- refs are epoch-scoped, so the whole cache (not just
    individual entries) is invalidated on a new snapshot, matching the
    plan's "cache ref -> Locator per ContextRef, invalidated on next
    navigate/snapshot".

    Handles both perception engines: the aria path records refs via `record`
    (aria-ref resolution + AX_NAME fallback), and the fusion path records the
    fused tree via `record_fused` (`e<backendNodeId>` resolution via the
    `MatchLevel` selector cascade). A given epoch uses one or the other."""

    epoch: int = 0
    _meta: dict[str, _RefMeta] = field(default_factory=dict)
    _locators: dict[str, Locator] = field(default_factory=dict)
    _fused: dict[str, EnhancedDOMTreeNode] = field(default_factory=dict)

    def reset(self, epoch: int) -> None:
        """Bumps the epoch and drops cached `Locator`s (page-bound, must be
        re-resolved). Deliberately does **not** clear `_meta`: a ref that
        isn't re-recorded by the new snapshot (the element moved, changed,
        or disappeared, so the new `aria_snapshot` didn't reissue that ref
        string for it) stays behind with its *old* epoch stamp -- that
        mismatch is exactly what lets `resolve()` tell "genuinely stale"
        apart from "never existed" below. A ref that *is* reissued for the
        same element (DOM unchanged) gets its `_meta` entry overwritten with
        the current epoch by `record()`, so it stays valid, as it should.

        The fusion `_fused` map *is* cleared: fusion refs are backendNodeIds
        keyed only to the tree just captured, and a superseded backendNodeId
        must not resolve against the new DOM."""

        self.epoch = epoch
        self._locators.clear()
        self._fused.clear()

    def record(self, ref: str, *, role: str, name: str) -> None:
        if ref:
            self._meta[ref] = _RefMeta(epoch=self.epoch, role=role, name=name)

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
        # Fusion path: `e<backendNodeId>` refs resolve through the MatchLevel
        # selector cascade (id -> data-testid -> xpath -> role+name -> attr),
        # all via Playwright's selector engine (no CDP Runtime).
        fused_node = self._fused.get(ref)
        if fused_node is not None:
            return await self._resolve_fused(page, ref, fused_node)

        meta = self._meta.get(ref)
        if meta is None:
            raise StaleRefError(ref, epoch_superseded=False)
        if meta.epoch != self.epoch:
            raise StaleRefError(ref, epoch_superseded=True)

        cached = self._locators.get(ref)
        if cached is not None:
            return cached

        exact = page.locator(f"aria-ref={ref}")
        try:
            if await exact.count() == 1:
                self._locators[ref] = exact
                return exact
        except Exception:
            pass  # fall through to the AX_NAME tier

        if meta.name:
            fallback = page.get_by_role(meta.role, name=meta.name, exact=True)  # type: ignore[arg-type]
            if await fallback.count() == 1:
                self._locators[ref] = fallback
                return fallback

        raise StaleRefError(ref, epoch_superseded=False)

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
