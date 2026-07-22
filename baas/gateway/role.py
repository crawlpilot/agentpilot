"""P2's role split: `--role gateway|worker`, same codebase (`plan.md`).
`monolith` (the default) is this repo's own addition -- P0/P1 never split
roles, and every existing deployment/test hits one process at `:8000`
expecting the full tenant-facing API; `monolith` preserves that unchanged
while `gateway`/`worker` become available as an explicit opt-in (see
`docker-compose.yml`'s `split` profile) rather than a breaking migration.

- `monolith`: owns the driver, registry, and reaper (today's P0/P1
  behavior) *and* serves `/v1/sessions/...` directly -- no HTTP hop to
  itself.
- `worker`: owns the driver/registry/reaper; serves only
  `/internal/sessions/...`, never `/v1/...` -- internal-only, VPC-bound,
  never tenant-exposed, per `plan.md`'s topology.
- `gateway`: stateless; serves `/v1/sessions/...` as a thin proxy to a
  configured worker's `/internal/...` surface. Never constructs a
  `PatchrightDriver` -- `baas.gateway.wiring` still imports `baas.driver` at
  module level (it's the composition root, exempt from the
  "only the composition root imports the driver" contract either way), but
  a gateway-role `Wiring` never instantiates it. A true "gateway image with
  no Chrome install" needs a separate Dockerfile target, which this pass
  doesn't build -- same image serves all three roles here.
"""

from __future__ import annotations

import os
from typing import Literal

Role = Literal["monolith", "gateway", "worker"]

_VALID_ROLES: tuple[Role, ...] = ("monolith", "gateway", "worker")


def get_role() -> Role:
    raw = os.environ.get("BAAS_ROLE", "monolith")
    if raw not in _VALID_ROLES:
        raise ValueError(f"BAAS_ROLE={raw!r} must be one of {_VALID_ROLES}")
    return raw  # type: ignore[return-value]
