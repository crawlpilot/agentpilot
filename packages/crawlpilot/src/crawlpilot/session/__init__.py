"""P1's session-management layer: registry (identity -> warm context),
lease lifecycle, and the reaper that destroys what P0 never did.

Never imports `crawlpilot.driver` -- it depends only on the `BrowserDriver`
Protocol and `spi` dataclasses, same rule as `agentpilot.gateway`. P2 replaces
`registry.py`'s in-memory dict with Redis + Lua behind the same interface.

`__all__` names the published **modules**. What is absent is as deliberate as
what is present: `acquire`, `browser_headers`, `http_fetch`, `lease` and
`stealth_profile` are internals of how a session is obtained and dressed, and a
consumer that reaches for one has coupled itself to a decision this package
intends to keep changing.
"""

from __future__ import annotations

from crawlpilot.session import ephemeral, interactive, reaper, registry, rotation, warm_pool

__all__ = [
    "ephemeral",
    "interactive",
    "reaper",
    "registry",
    "rotation",
    "warm_pool",
]
