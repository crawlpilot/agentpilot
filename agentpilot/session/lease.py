"""Lease lifecycle helpers -- pure functions over `spi.lease.Lease`.

Renewal is a sliding window: `acquired_at` is bumped forward instead of
tracking a separate `expires_at` field, so `spi.lease.Lease`'s P0 shape
doesn't need to change for P1 to add renewal. `registry.py` is the only
caller; kept as free functions (not methods) so it's trivially unit-testable
without spinning up a `Registry`.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, Lease, LeaseId


def new_lease(
    identity: IdentityKey, owner: str, ttl_seconds: float, context_ref: ContextRef
) -> Lease:
    return Lease(
        lease_id=LeaseId(str(uuid.uuid4())),
        identity=identity,
        owner=owner,
        acquired_at=datetime.now(UTC),
        ttl_seconds=ttl_seconds,
        context_ref=context_ref,
    )


def renew(lease: Lease) -> Lease:
    return replace(lease, acquired_at=datetime.now(UTC))


def is_expired(lease: Lease, *, now: datetime | None = None) -> bool:
    now = now if now is not None else datetime.now(UTC)
    return (now - lease.acquired_at).total_seconds() >= lease.ttl_seconds
