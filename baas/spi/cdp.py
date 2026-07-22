"""Optional capability: exposing a context's local Chrome DevTools Protocol
endpoint, for external CDP clients (Playwright's `connect_over_cdp`,
Puppeteer, browser-use, ...) to attach directly. Mirrors `baas.spi.streaming`'s
`LiveViewCapable` pattern: checked via `isinstance` at the composition root,
a driver without it simply doesn't get the CDP routes wired up.

Deliberately not subject to `live_view.py`'s "Page/Input only, never Runtime"
constraint -- a session opened with `enable_cdp=True` trades away Patchright's
anti-detection guarantee for that one session, by design (see plan.md's CDP
feature decision).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from baas.spi.lease import ContextRef


@runtime_checkable
class CdpEndpointCapable(Protocol):
    async def cdp_http_base(self, ctx: ContextRef) -> str | None:
        """Local (loopback-only) CDP HTTP base, e.g. `'http://127.0.0.1:9222'`,
        from which both `/json/version` and the browser-level
        `webSocketDebuggerUrl` can be resolved. `None` if this context wasn't
        opened with `enable_cdp=True`."""
        ...
