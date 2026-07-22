"""Driver-agnostic (and gateway-agnostic) API-key auth types -- kept
separate from `baas.gateway` so import-linter's layering holds: `baas.auth`
sits alongside `baas.identity`/`baas.session`, below `baas.gateway`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    tenant: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class AuthedTenant:
    """The result of a successful tenant-auth check -- the tenant a request
    is authenticated as, resolved from its API key rather than trusted from
    a free-text request-body field (see `baas.gateway.auth_deps`)."""

    tenant: str
    key_id: str
