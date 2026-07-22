"""The seam: a driver-agnostic Protocol, narrowed to batch execution.

Single-verb convenience calls are thin sugar over `execute(ctx, [SingleAction])`
-- never a second code path a driver implements separately. `runtime_checkable`
lets the composition root `isinstance`-assert at startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from baas.spi.actions import Action, ActionResult
from baas.spi.egress import EgressPolicy
from baas.spi.health import HealthStatus
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef
from baas.spi.proxy import ProxyEndpoint
from baas.spi.storage_state import StorageState


@runtime_checkable
class BrowserDriver(Protocol):
    async def open(
        self,
        identity: IdentityKey,
        profile_dir: Path,
        proxy: ProxyEndpoint | None,
        headful: bool,
        egress: EgressPolicy,
        block_popups: bool = False,
        enable_cdp: bool = False,
    ) -> ContextRef: ...

    async def close(self, ctx: ContextRef) -> None: ...

    async def execute(
        self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
    ) -> ActionResult:
        """`page_id` selects which tab within `ctx`'s context this whole
        batch dispatches against; `None` means "the context's current active
        tab" -- every pre-multi-tab caller keeps working unchanged. Storage
        state (`export_state`/`restore_state` below) stays context-scoped,
        not page-scoped: cookies/localStorage are a browser-context-wide
        concept in Playwright, not per-tab."""
        ...

    async def export_state(self, ctx: ContextRef) -> StorageState: ...

    async def restore_state(self, ctx: ContextRef, state: StorageState) -> None: ...

    async def health(self, ctx: ContextRef) -> HealthStatus: ...
