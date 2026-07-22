"""P2's identity layer: profile-dir path safety, encrypted-at-rest state
vaulting, and assign-once proxy pinning. Never imports `baas.driver` -- same
composition-root rule as `baas.gateway`/`baas.session`."""

from __future__ import annotations
