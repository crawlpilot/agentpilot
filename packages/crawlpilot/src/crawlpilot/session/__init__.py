"""P1's session-management layer: registry (identity -> warm context),
lease lifecycle, and the reaper that destroys what P0 never did.

Never imports `agentpilot.driver` -- it depends only on the `BrowserDriver`
Protocol and `spi` dataclasses, same rule as `agentpilot.gateway`. P2 replaces
`registry.py`'s in-memory dict with Redis + Lua behind the same interface.
"""

from __future__ import annotations
