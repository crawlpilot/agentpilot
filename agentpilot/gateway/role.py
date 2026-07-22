"""P2's role split: `--role gateway|worker`, same codebase (`plan.md`).
`monolith` (the default) predates the split and is no longer a
docker-compose deployment target (`docker-compose.yml`'s default topology
is gateway+worker, unconditionally, each with a purpose-built image) -- but
it remains a valid `AGENTPILOT_ROLE` value for non-Docker use: `uv run uvicorn
agentpilot.gateway.app:app` locally, and the implicit default for this repo's own
unit test suite, most of which never sets `AGENTPILOT_ROLE` and so gets
`monolith` from `get_role()` below.

- `monolith`: owns the driver, registry, and reaper (today's P0/P1
  behavior) *and* serves `/v1/sessions/...` directly -- no HTTP hop to
  itself.
- `worker`: owns the driver/registry/reaper; serves only
  `/internal/sessions/...`, never `/v1/...` -- internal-only, VPC-bound,
  never tenant-exposed, per `plan.md`'s topology. Built from
  `docker/worker.Dockerfile`.
- `gateway`: stateless; serves `/v1/sessions/...` as a thin proxy to a
  configured worker's `/internal/...` surface. Never constructs a
  `PatchrightDriver` -- `agentpilot.gateway.wiring` still imports `agentpilot.driver` at
  module level (it's the composition root, exempt from the
  "only the composition root imports the driver" contract either way), but
  a gateway-role `Wiring` never instantiates it. Built from `docker/
  gateway.Dockerfile`, a genuinely Chrome-free image (no Xvfb/X11/iptables,
  no `patchright install`) -- the gap this docstring used to flag ("same
  image serves all three roles") is closed.
"""

from __future__ import annotations

import os
from typing import Literal

Role = Literal["monolith", "gateway", "worker"]

_VALID_ROLES: tuple[Role, ...] = ("monolith", "gateway", "worker")


def get_role() -> Role:
    raw = os.environ.get("AGENTPILOT_ROLE", "monolith")
    if raw not in _VALID_ROLES:
        raise ValueError(f"AGENTPILOT_ROLE={raw!r} must be one of {_VALID_ROLES}")
    return raw
