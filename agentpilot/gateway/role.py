"""P2's role split: `AGENTPILOT_ROLE=gateway|worker`, same codebase (`plan.md`).
The single-process `monolith` role was removed -- the only deployment topology
is gateway+worker (`docker-compose.yml`), each with a purpose-built image.

- `worker`: owns the driver/registry/reaper; serves only
  `/internal/sessions/...`, never `/v1/...` -- internal-only, VPC-bound,
  never tenant-exposed, per `plan.md`'s topology. Runs the crawl/agent/recipe
  worker loops. Built from `docker/worker.Dockerfile`.
- `gateway`: stateless; serves the tenant-facing `/v1/...` surface -- a thin
  proxy to a worker's `/internal/...` for driver-backed routes, and directly
  for job/run CRUD (which never touches `agentpilot.driver`). Never constructs
  a `PatchrightDriver`. Built from `docker/gateway.Dockerfile`, a Chrome-free
  image (no Xvfb/X11/iptables, no `patchright install`).

`AGENTPILOT_ROLE` defaults to `gateway` (the module is `gateway.app`, and a
bare `uvicorn agentpilot.gateway.app:app` is a gateway); compose sets it
explicitly on every service.
"""

from __future__ import annotations

import os
from typing import Literal

Role = Literal["gateway", "worker"]

_VALID_ROLES: tuple[Role, ...] = ("gateway", "worker")


def get_role() -> Role:
    raw = os.environ.get("AGENTPILOT_ROLE", "gateway")
    if raw not in _VALID_ROLES:
        raise ValueError(f"AGENTPILOT_ROLE={raw!r} must be one of {_VALID_ROLES}")
    return raw  # type: ignore[return-value]
