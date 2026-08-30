"""Driver-agnostic lease/context types. Never holds a Playwright object."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import NewType

from crawlpilot.spi.identity import IdentityRef

LeaseId = NewType("LeaseId", str)


class ContextState(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    RETIRING = "retiring"
    RETIRED = "retired"


@dataclass
class ContextRef:
    """Reference to a live browser context. `node_id` is always `"local"` in
    P0 (single-node); the field exists now so P2's placement layer doesn't
    need a breaking shape change."""

    context_id: str
    identity: IdentityRef
    state: ContextState
    pid: int | None
    node_id: str = "local"


@dataclass
class Lease:
    lease_id: LeaseId
    identity: IdentityRef
    owner: str
    acquired_at: datetime
    ttl_seconds: float
    context_ref: ContextRef
