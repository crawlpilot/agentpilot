"""P2's identity layer: profile-dir path safety, encrypted-at-rest state
vaulting, and assign-once proxy pinning. Never imports `agentpilot.driver` -- same
composition-root rule as `agentpilot.gateway`/`agentpilot.session`."""

from __future__ import annotations
