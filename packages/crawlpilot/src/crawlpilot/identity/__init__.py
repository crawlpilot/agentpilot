"""P2's identity layer: profile-dir path safety, encrypted-at-rest state
vaulting, and assign-once proxy pinning. Never imports `crawlpilot.driver` -- same
composition-root rule as `agentpilot.gateway`/`crawlpilot.session`."""

from __future__ import annotations
