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

from patchright.async_api import Locator, Page

from baas.spi.errors import StaleRefError


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
    navigate/snapshot"."""

    epoch: int = 0
    _meta: dict[str, _RefMeta] = field(default_factory=dict)
    _locators: dict[str, Locator] = field(default_factory=dict)

    def reset(self, epoch: int) -> None:
        self.epoch = epoch
        self._meta.clear()
        self._locators.clear()

    def record(self, ref: str, *, role: str, name: str) -> None:
        if ref:
            self._meta[ref] = _RefMeta(epoch=self.epoch, role=role, name=name)

    async def resolve(self, page: Page, ref: str) -> Locator:
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
