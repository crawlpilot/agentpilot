"""The seam: a driver-agnostic Protocol, narrowed to batch execution.

Single-verb convenience calls are thin sugar over `execute(ctx, [SingleAction])`
-- never a second code path a driver implements separately. `runtime_checkable`
lets the composition root `isinstance`-assert at startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from agentpilot.spi.actions import Action, ActionResult
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.health import ContextHealth, HealthStatus
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef
from agentpilot.spi.proxy import ProxyEndpoint
from agentpilot.spi.storage_state import StorageState


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
        locale: str | None = None,
        timezone_id: str | None = None,
    ) -> ContextRef:
        """`locale`/`timezone_id` (when set) override the browser context's
        reported `navigator.language`/`Accept-Language` and JS timezone --
        an anti-detection consistency lever (a US retail site expects a
        plausible US locale/timezone, not whatever the host container
        happens to run as). `None` leaves Chrome's own defaults untouched,
        so existing interactive/test callers are unaffected."""
        ...

    async def close(self, ctx: ContextRef) -> None: ...

    async def is_alive(self, ctx: ContextRef) -> bool:
        """Fast, non-blocking liveness: the context's process still exists AND
        it answers a cheap, bounded CDP ping (responsive, not just present).
        The session layer calls this to validate a *reused* warm/IDLE context
        before handing it out -- a `False` triggers evict + reopen rather than
        letting a zombie Chrome surface as a failed action mid-request."""
        ...

    async def keepalive(self, ctx: ContextRef) -> bool:
        """Nudge an idle context's CDP connection so an intermediate proxy
        doesn't silently drop it during long idle periods (the analog of
        agent-browser's WebSocket ping + TCP `SO_KEEPALIVE`). Returns whether
        the context is still responsive; a `False` lets the keepalive loop
        evict it so the next acquire auto-restarts a fresh one."""
        ...

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

    async def context_health(self, ctx: ContextRef) -> ContextHealth | None:
        """Per-context health tallies for the session layer's retire/rotate
        decision, or `None` if the context is unknown. Distinct from `health`
        (is the context *alive*) -- this is *how well* it's doing."""
        ...
